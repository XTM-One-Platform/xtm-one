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

"""What a render's checks found, and the gate a new render passes.

A render hears and watches what it made (``voice_check``, ``audio_qc``,
``frame_freeze``); every problem is a :class:`Finding` with its time in the
render's own MP4 and its scene, stored on the render (``VideoRender.checks``,
:class:`Checks`) and said by ``render_video``, ``render_video_scene`` and
``get_video_jobs`` (:func:`format_checks`).

* An audio finding (a narration that failed its transcript check, the
  voice-over heard back with too many wrong words, a lexicon term heard wrong
  in the final voice, a sentence heard twice, a stutter) is a failure:
  a whole render with one fails, its MP4 kept to look at, never made what the
  video plays - unless it was forced. Its ``pronunciation`` section says, per
  lexicon term, how often the voice says it, how often as the lexicon does,
  and what was heard - and each take it failed in that the render does not
  play (replaced by a take that passed, never counted as 100 %).
* A narration the closest voice of its speech model said in the video's place
  (``voice_fallback``) is a failure too: two narrators
  in one film, which the audience hears. It names the scene, that voice, why
  and what to change (reword, a pronunciation entry, or that voice made the
  video's); a whole render stops on it before drawing, as on a narration
  that failed, and a forced one plays that voice with the finding.
* A frozen picture is a warning: the render is ready, the finding stays open.
* A picture finding nothing in the spec causes (an insert missing or dropped on frames the spec shows it on,
  ``action: nothing``) is the render harness's defect (:func:`harness_defects`): never a finding - it
  neither blocks nor warns, on any drawing -, kept on the render with its frame (``harness``) and said once to the
  administrator; a render is deterministic, and no edit of the video moves it.
* How loud the music plays (``music_level``) is the audio check's
  ``music`` section - its integrated loudness, its level under the voice once
  ducked, its longest quiet stretch - and a warning when the mix loses it.

:func:`gate` is what ``projects.start_render`` asks before a whole render
starts. It refuses when an AI shot or a recording a scene plays is not ready,
when the quality check saved for the revision has a finding the
audience would see (:data:`BLOCKING_SEVERITIES`), when a narration of
the video failed its last check, when the previous render of the same
revision left a failing finding open (or, at the same quality, a frozen
picture: the same video again), or when a scene changed since a preview or a
whole render last drew it (:func:`scene_digests`, kept on the video by
:func:`record_seen`). A drawing is kept by what it drew: the last
:data:`DRAWINGS_KEPT` digests of each scene, so an edit reverted finds the
drawing of what the scene shows again. A scene preview is evidence the gate
trusts: a finding about an AI shot is cleared once every scene
showing the shot passed a preview drawn as it is now, and a finding a
previous render left on a scene is cleared once a newer drawing of that
scene passed. ``force`` starts it
anyway; the override is kept on the render (``gate_override``), logged and
audited, and a forced render places its failed narrations and keeps its
findings rather than failing on them.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

FAIL, WARN = "fail", "warn"
PASSED, FAILED, FINDINGS, SKIPPED = "passed", "failed", "findings", "skipped"
#: The kinds of finding, as stored and said.
VOICE_SEGMENT, TRANSCRIPT, PRONUNCIATION, REPEATED_SENTENCE, STUTTER, FROZEN = (
    "voice_segment",
    "transcript",
    "pronunciation",
    "repeated_sentence",
    "stutter",
    "frozen",
)
#: A narration another voice said after the video's failed: the closest of its speech model, a failure
#: like a narration that failed.
VOICE_FALLBACK = "voice_fallback"
AUDIO_KINDS = frozenset({VOICE_SEGMENT, VOICE_FALLBACK, TRANSCRIPT, PRONUNCIATION, REPEATED_SENTENCE, STUTTER})
#: The whole frame dimming for a moment outside any transition (``brightness_dip``): a warning held like a
#: frozen picture - it refuses another render of the same quality until what dims it is changed or previewed again.
BRIGHTNESS_DIP = "brightness_dip"
#: A recording frame of the render drawn as a mix of the two around it (``double_frames``): a ghost of the
#: page made anywhere in the pipeline - a warning held like a frozen picture, its frame stored with it.
DOUBLE_IMAGE = "double_image"
#: A recording scene's static page chrome (a toolbar, a button over a canvas) drawn at another place and back on
#: consecutive frames (``static_chrome``, issue #9590): a ghost of the page's own chrome, a failure of the picture.
STATIC_FLICKER = "static_flicker"
#: A recording's canvas left near empty for longer than a beat, the cursor alone moving (``near_empty``): a step
#: that hid what it showed - a warning held like a frozen picture, its frame stored with it and a trim proposed.
NEAR_EMPTY = "near_empty"
#: The warnings a render of the same quality is refused on (:func:`open_findings`), each a scene's preview clears.
HELD_WARNINGS = frozenset({FROZEN, BRIGHTNESS_DIP, DOUBLE_IMAGE, NEAR_EMPTY})
#: How loud the music plays (``music_level``): silent, too quiet, a long quiet stretch, lost under the voice.
#: A warning: the render is ready, the person hears it and decides.
MUSIC_LEVEL = "music_level"
#: What the mixed audio measured (``mix_check``): the music ending early or looping with a seam, a narration
#: read too slowly or too fast, a silent gap in a narrated scene, a still picture over silence, a clipping peak.
#: A warning: the render is ready, the person hears it and decides.
AUDIO_MIX = "audio_mix"
#: What the screen inserts showed (``insert_checks``): drawn over the foreground, a picture padded, a brand
#: mark lost, a tracked surface lost, an insert not drawn at all, its colours shifted (its blacks lifted, its
#: hue turned or a cast, against its source). Failures like the audio's: the spec and the media decide them, not the
#: quality.
INSERT_FOREGROUND, INSERT_PADDED, LOGO_LOST, SURFACE_LOST = (
    "insert_foreground",
    "insert_padded",
    "logo_lost",
    "surface_lost",
)
INSERT_MISSING, INSERT_COLOUR_SHIFT = "insert_missing", "insert_colour_shift"
#: An AI shot whose composited logo shows more than once in a frame (``logos.duplicate_findings``).
LOGO_DUPLICATED = "logo_duplicated"
#: An AI shot whose logo drawn onto the take reads as a flat sticker
#: (``logos.integration_findings``): lit flat on a shaded surface, moving rigidly or keeping its size on one that bends.
LOGO_NOT_INTEGRATED = "logo_not_integrated"
#: The brand's mark in two places of one drawn frame (``logos.brand_marks_on``): the corner watermark over
#: footage that shows the logo itself, or two instances of any mark - on every scene, the render's and the preview's.
DUPLICATE_LOGO = "duplicate_logo"
#: An AI shot playing lettering its take invented, read by the render when the take kept no text check
#: (``text_backfill.render_findings``, issue #9021): letters that make no word, its crop as its frame.
INVENTED_TEXT = "invented_text"
#: What the audience would see of a screen insert's compositing (``insert_checks``): its quad off the bezel
#: the footage shows, the key left in or around the surface, a matte or a despill that flickers (or a matte
#: toggling back at its edge), an insert drawn soft (or a source with too few pixels for its quad
#: at the render's size, a replaced screen's screenshot included), a halo along the foreground's edge.
INSERT_ALIGNMENT, INSERT_RESIDUAL_KEY, INSERT_FLICKER = "insert_alignment", "insert_residual_key", "insert_flicker"
INSERT_SOFT, INSERT_HALO = "insert_soft", "insert_halo"
#: A quad that jumps from one frame to the next beyond the motion of the frames around it (``insert_checks.quad_jitter``):
#: a blinking screen however close to its bezel it stays on average.
INSERT_JITTER = "insert_jitter"
#: A grey rim on the surface side of what stands in front (``screen_insert.FrameAudit.rim``): the key's residue a
#: dark edge leaves, which no hue gate reads.
INSERT_RIM = "insert_rim"
#: A ``cover`` fit cutting the source's own content to fill the quad: a sidebar, a right column goes.
INSERT_CROPPED = "insert_cropped"
#: An unkeyed ``contain`` insert whose bands, left to the footage, show a chroma key colour (``FrameAudit.band_key``):
#: two bars of the screen's green a viewer sees, never the fit's design.
INSERT_BAND_KEY_COLOUR = "insert_band_key_colour"
#: A surface the frames show curved (``surface_refine.Flatness``: an edge bowing past 5%, its points confirming
#: it) - the save's ``insert_flatness`` read on the frames drawn: a tracked insert's too, which no save measures.
INSERT_FLATNESS = "insert_flatness"
#: A keyed surface showing its key on an encoded frame of a join between two scenes (``insert_checks.dropped``):
#: the insert not drawn there, read once the render is encoded - never a green verdict.
INSERT_DROPPED = "insert_dropped"
#: Two screen inserts placed back to back on one surface leaving it empty between them (``insert_checks.insert_gaps``):
#: the first gone and the next not yet in on one frame or more, the surface shown bare at the join.
INSERT_GAP = "insert_gap"
#: An insert the render did not measure where it is drawn (``insert_checks.unmeasured``, ``PresenceGate``): the
#: bezel its save or its scene's preview found is found by no frame of the render, or the encoded frame shows its source
#: off the quad the gate reads - every reading of it is of another region, never a fact of the video.
INSERT_MEASUREMENT_LOST = "insert_measurement_lost"
#: What stands in front of a screen insert's surface as the audience sees it (``insert_checks``): the source's own
#: strokes drawn across it (``screen_insert.through_share``), a mask too unsure of it on enough frames to be trusted
#: (a ghost: ``FrameAudit.ghosted``), and the key's hue spilled past the quad on the footage (``screen_insert.key_spill``).
INSERT_THROUGH_FOREGROUND, INSERT_GHOST, KEY_SPILL_OUTSIDE_QUAD = (
    "insert_through_foreground",
    "insert_ghost",
    "key_spill_outside_quad",
)
#: An unkeyed insert's mask past its subject (``screen_insert.matte_read``): the surface left showing inside it
#: beside the subject (a dark halo, a ghost of the subject), its centroid lagging the subject's; and a legible insert
#: whose quad what stands in front hides too much of (``FrameAudit.foreground_share``).
INSERT_MATTE_HALO, INSERT_MATTE_LAG, INSERT_OCCLUDED = "insert_matte_halo", "insert_matte_lag", "insert_occluded"
PICTURE_KINDS = frozenset(
    {
        STATIC_FLICKER,
        INSERT_FOREGROUND,
        INSERT_THROUGH_FOREGROUND,
        INSERT_MATTE_HALO,
        INSERT_MATTE_LAG,
        INSERT_OCCLUDED,
        INSERT_GHOST,
        KEY_SPILL_OUTSIDE_QUAD,
        INSERT_MEASUREMENT_LOST,
        INSERT_PADDED,
        LOGO_LOST,
        SURFACE_LOST,
        INSERT_MISSING,
        INSERT_COLOUR_SHIFT,
        LOGO_DUPLICATED,
        LOGO_NOT_INTEGRATED,
        DUPLICATE_LOGO,
        INVENTED_TEXT,
        INSERT_ALIGNMENT,
        INSERT_JITTER,
        INSERT_RESIDUAL_KEY,
        INSERT_FLICKER,
        INSERT_SOFT,
        INSERT_HALO,
        INSERT_RIM,
        INSERT_CROPPED,
        INSERT_BAND_KEY_COLOUR,
        INSERT_FLATNESS,
        INSERT_DROPPED,
        INSERT_GAP,
    }
)
CHECKS_VERSION = 1
#: The findings a tool answer lists; the rest are counted.
LISTED_FINDINGS = 20
#: What to change for a finding of each kind whose maker names no fix of its own (:attr:`Finding.fix`).
KIND_FIXES = {
    VOICE_SEGMENT: "reword the narration or change its voice, then preview the scene (render_video_scene)",
    TRANSCRIPT: "reword the narration or change its voice, then preview the scene (render_video_scene)",
    PRONUNCIATION: "add or correct the term's entry in the pronunciation lexicon, then preview the scene",
    REPEATED_SENTENCE: "remove the sentence said twice from the narration, then preview the scene",
    STUTTER: "speak the narration again (edit it, or change its voice), then preview the scene",
    VOICE_FALLBACK: "change the video's voice or reword the narration until its own voice passes, then preview the scene",
    FROZEN: "give the footage more length, or its extend_mode zoom or loop",
    BRIGHTNESS_DIP: "preview the scene again; report the render with its frame if its spec asks for no dim there",
    DOUBLE_IMAGE: "preview the scene again; report the render with its frame if it still shows two pictures at once",
    STATIC_FLICKER: "record the take again on the current recorder, then preview the scene; report the render with its frame if it still flickers",
    NEAR_EMPTY: "trim the scene so it ends before the step that empties the canvas, or record that step again",
    MUSIC_LEVEL: "raise music.volume or choose another track, as its message says",
    AUDIO_MIX: "change the music, the narration or the scene as its message says, then preview the scene",
    SURFACE_LOST: "give the quad on calmer footage, cut the insert short before that moment, or drop track for a static quad",
    INSERT_MISSING: "give the insert a source the render can draw: what its message says is missing",
    LOGO_DUPLICATED: "generate the shot again in place (clip_id)",
    LOGO_NOT_INTEGRATED: (
        "generate the shot again with a model that takes the logo as a picture, or accept it flat "
        '(ingredients_quality: "flat")'
    ),
    INSERT_MEASUREMENT_LOST: "report the render with this frame: its insert was not measured where it is drawn",
    DUPLICATE_LOGO: "turn the watermark off on that scene (watermark: false), or generate the shot again with one logo",
}


def finding_fix(finding: dict) -> str:
    """What to change for a stored *finding*: its own fix, else its kind's (:data:`KIND_FIXES`), else what
    its message says."""
    return str(finding.get("fix") or KIND_FIXES.get(str(finding.get("kind"))) or "what its message names")


def clock(seconds: float | None) -> str:
    """``1:02.35``: a time in the MP4 as a player shows it, to the hundredth."""
    if seconds is None:
        return "?"
    minutes, rest = divmod(max(0.0, float(seconds)), 60.0)
    return f"{int(minutes)}:{rest:05.2f}"


@dataclass(frozen=True)
class Finding:
    kind: str
    #: Seconds into the render's MP4 where it starts, and ends.
    at: float
    end: float | None
    scene_id: str | None
    message: str
    severity: str = FAIL
    data: dict = field(default_factory=dict)
    #: What to change for it, the end of its :attr:`message` - what a render's answer lists it with
    #: (``render_answer``); ``None``: the kind's own (:data:`KIND_FIXES`).
    fix: str | None = None
    #: What the editor words it with in the reader's language: a key of ``FINDING_MESSAGES`` in
    #: ``video-format.ts`` and its values. :attr:`message` stays the English an agent reads, and what a build that
    #: does not know the code shows. ``None``: the message alone.
    code: str | None = None
    params: dict | None = None

    def as_dict(self) -> dict:
        out: dict[str, Any] = {
            "kind": self.kind,
            "severity": self.severity,
            "at": round(self.at, 3),
            "end": round(self.end, 3) if self.end is not None else None,
            "scene_id": self.scene_id,
            "message": self.message,
        }
        if self.data:
            out["data"] = self.data
        if self.fix:
            out["fix"] = self.fix
        if self.code:
            out["code"] = self.code
            out["params"] = dict(self.params or {})
        return out


def scene_at(document: dict, seconds: float) -> dict | None:
    """The planned scene on screen at *seconds* of *document*: the later one where two overlap (a transition)."""
    found = None
    for scene in document.get("scenes") or []:
        if float(scene.get("start") or 0.0) <= seconds:
            found = scene
    return found


@dataclass
class Checks:
    """Everything a render's checks found: what :attr:`VideoRender.checks` stores."""

    voice: list[dict] = field(default_factory=list)
    audio: dict = field(default_factory=dict)
    frames: dict = field(default_factory=dict)
    #: What the cut gate read (``cut_blend.CutCheck.as_summary``, issue #8935): ``{status, fps, joins_read, joins,
    #: skipped, findings, read_on}``, or ``{status: skipped, reason}`` when it could not run; empty when it never ran.
    cuts: dict = field(default_factory=dict)
    #: The lexicon's terms in the final voice: ``{status, terms: [{term, occurrences, passed, heard}]}``, each
    #: with the takes it failed in that the render does not play (``attempts_failed: [{scene_id, times, heard}]``).
    pronunciation: dict = field(default_factory=dict)
    findings: list[Finding] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    #: What each screen insert's compositing measured (``insert_checks.reports``): its refined quad, seed
    #: error, residual key, flicker, sharpness, halo, its colours at its light, the resolution its numbers
    #: and its crops come from, and the 100 % crops of its region (``crops: [{frame, at, word, box, file_id}]``),
    #: one entry per insert drawn.
    inserts: list[dict] = field(default_factory=list)
    #: What the render harness drew wrong (:func:`harness_defects`): the findings nothing in the spec causes,
    #: kept with their frame, said once to the administrator - never a finding of the video.
    harness: list[Finding] = field(default_factory=list)
    #: What the platform's own measurements disagreed on (``insert_checks.bezel_readings``): a bezel reading
    #: set aside for the save's, said to the administrator - never a finding of the video.
    infra: list[str] = field(default_factory=list)
    #: Who judged these checks (:func:`report_provenance`): the quality report version, the release and the time
    #: they were judged at. ``None``: the running release, now - what a render judging its frames records; a report
    #: read back (:meth:`from_dict`) keeps the ones it was judged with.
    report_version: int | None = None
    build: str | None = None
    checked_at: str | None = None
    #: The screen inserts' drawing these frames were drawn at (``screen_insert.DRAWING_VERSION``); ``None``: the
    #: running release's, like :attr:`report_version`. Said beside it in every answer (dev:video-drawing-version-in-answers).
    drawing_version: int | None = None

    @property
    def failed(self) -> bool:
        return any(f.severity == FAIL for f in self.findings)

    @property
    def status(self) -> str:
        if self.failed:
            return FAILED
        if self.findings:
            return FINDINGS
        ran = self.audio.get("status") not in (None, SKIPPED) or self.frames.get("status") not in (None, SKIPPED)
        return PASSED if ran else SKIPPED

    @classmethod
    def from_dict(cls, payload: dict | None) -> Checks:
        """The checks a render stored (:meth:`as_dict`), read back: what a preview served from a kept one carries
        (``kept_previews.serve``)."""
        payload = payload if isinstance(payload, dict) else {}

        def read(item: dict) -> Finding:
            return Finding(
                kind=str(item.get("kind") or ""),
                at=float(item.get("at") or 0.0),
                end=float(item["end"]) if item.get("end") is not None else None,
                scene_id=item.get("scene_id"),
                message=str(item.get("message") or ""),
                severity=str(item.get("severity") or FAIL),
                data=dict(item.get("data") or {}),
                fix=item.get("fix"),
                code=item.get("code") or None,
                params=dict(item.get("params") or {}) if item.get("code") else None,
            )

        return cls(
            voice=list(payload.get("voice") or []),
            audio=dict(payload.get("audio") or {}),
            frames=dict(payload.get("frames") or {}),
            cuts=dict(payload.get("cuts") or {}),
            pronunciation=dict(payload.get("pronunciation") or {}),
            findings=[read(item) for item in findings_of(payload)],
            notes=list(payload.get("notes") or []),
            inserts=list(payload.get("inserts") or []),
            harness=[read(item) for item in harness_of(payload)],
            infra=[str(line) for line in payload.get("infra") or []],
            report_version=_int_or_none(payload.get("quality_report_version")),
            build=str(payload["build"]) if payload.get("build") else None,
            checked_at=str(payload["checked_at"]) if payload.get("checked_at") else None,
            drawing_version=_int_or_none(payload.get("drawing_version")),
        )

    def as_dict(self) -> dict:
        from datetime import UTC, datetime

        from app.core.config import settings
        from app.services.video_studio import quality, screen_insert

        out = {
            "version": CHECKS_VERSION,
            # The quality report version these checks were judged at (dev:render-picture-check-gate-drift-2): a render
            # resumed across deploys is judged once, at the end, by the release that finishes it. The release and the
            # time say which build that was (dev:video-jobs-quality-report-version).
            "quality_report_version": self.report_version or quality.QUALITY_REPORT_VERSION,
            # The screen inserts' drawing its frames were drawn at: whether a merged compositing fix drew them.
            "drawing_version": self.drawing_version or screen_insert.DRAWING_VERSION,
            "build": self.build or settings.app_version or None,
            "checked_at": self.checked_at or datetime.now(UTC).isoformat(timespec="seconds"),
            "status": self.status,
            "voice": self.voice,
            "audio": self.audio,
            "frames": self.frames,
            "cuts": self.cuts,
            "pronunciation": self.pronunciation,
            "findings": [f.as_dict() for f in sorted(self.findings, key=lambda f: (f.at, f.kind))],
            "notes": self.notes[:20],
        }
        if self.inserts:
            out["inserts"] = self.inserts
        if self.harness:
            out["harness"] = [f.as_dict() for f in sorted(self.harness, key=lambda f: (f.at, f.kind))]
        if self.infra:
            out["infra"] = self.infra[:20]
        return out


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def report_provenance(checks: dict | None, relaunches: list | None = None, drawing: dict | None = None) -> dict | None:
    """Which checker judged a render's stored *checks*, and whether that is the running one. Pure but for the
    running release and quality report version it compares with; ``None`` when the render stored no checks.

    ``{quality_report_version, drawing_version, live_drawing_version, drawing_current, build, checked_at, current,
    resumed_across_builds, recomputed_after_resume}``: *drawing_version* is the screen inserts' drawing the checks
    recorded, else the one a whole render's *drawing* (``VideoRender.drawing``) recorded, ``None`` when neither did;
    *drawing_current* is ``False`` when it is older than the running ``screen_insert.DRAWING_VERSION``.
    *build* and *checked_at* are ``None`` on a report stored before they were recorded. A render's checks run once,
    at its end, on every frame it plays - the frames kept from an attempt on an older release included -, so a render
    resumed across deploys (its *relaunches* name another release) is judged by the release that finished it:
    ``recomputed_after_resume`` says so, and ``resumed_across_builds`` lists the releases its frames were drawn by.
    *current* is ``False`` when the checker changed since the report (a lower quality report version than the
    running one): its findings are those of an older checker, and a new render is judged anew.
    """
    from app.services.video_studio import quality, screen_insert

    if not isinstance(checks, dict) or not checks:
        return None
    version = _int_or_none(checks.get("quality_report_version"))
    release = drawing.get("release") if isinstance(drawing, dict) else None
    drawn_at = _int_or_none(checks.get("drawing_version"))
    if drawn_at is None and isinstance(release, dict):
        drawn_at = _int_or_none(release.get("drawing"))
    build = str(checks.get("build") or "") or None
    drawn_by = list(
        dict.fromkeys(
            str(entry.get("version"))
            for entry in relaunches or []
            if isinstance(entry, dict) and entry.get("version") and str(entry.get("version")) != build
        )
    )
    found = {
        "quality_report_version": version,
        "drawing_version": drawn_at,
        "live_drawing_version": screen_insert.DRAWING_VERSION,
        "drawing_current": drawn_at is not None and drawn_at >= screen_insert.DRAWING_VERSION,
        "build": build,
        "checked_at": str(checks.get("checked_at") or "") or None,
        "current": version is not None and version >= quality.QUALITY_REPORT_VERSION,
        "resumed_across_builds": drawn_by,
        "recomputed_after_resume": bool(drawn_by) and build is not None,
    }
    from app.services.video_studio import render_rejudge

    # Judged again since, nothing drawn (``render_rejudge``): the newest re-judge beside the report it was judged with.
    if (again := render_rejudge.latest(checks)) is not None:
        rejudged_at = _int_or_none(again.get("quality_report_version"))
        found["rejudged"] = {
            "quality_report_version": rejudged_at,
            "current": rejudged_at is not None and rejudged_at >= quality.QUALITY_REPORT_VERSION,
            "checked_at": again.get("checked_at"),
            "build": again.get("build"),
            "status": again.get("status"),
            "findings": len(again.get("findings") or []),
            "sampled": list(again.get("sampled") or []),
        }
    return found


def judged_before(version: Any) -> int | None:
    """The quality report *version* a finding was judged under when it is older than the running checker's
    (``quality.QUALITY_REPORT_VERSION``); ``None`` when it is current or was not recorded."""
    from app.services.video_studio import quality

    judged = _int_or_none(version)
    return judged if judged is not None and judged < quality.QUALITY_REPORT_VERSION else None


def re_preview_words(judged: int | None, scene_id: str) -> str:
    """What a gate reason says of a scene's failed drawing an older checker (report *judged*,
    :func:`judged_before`; ``None`` when no version was recorded and its render no longer says one) judged: not a
    defect to fix, a drawing to judge again under the running report."""
    from app.services.video_studio import quality

    running = quality.QUALITY_REPORT_VERSION
    by = f"quality report v{judged}" if judged is not None else "a quality report version that was not recorded"
    return (
        f"judged under {by}, older than the running v{running}: "
        f"nothing to fix yet, re-preview under report v{running} "
        f"(render_video_scene(scene='{scene_id}')) and act on what that preview finds"
    )


def failed_drawing_words(version: int | None, scene_id: str) -> str:
    """What the gate says after a scene's failed picture check, given the quality report *version* its drawing was
    judged under (``None``: not recorded, and its render row gone or silent). Every drawing the running checker
    records names its version, so an unrecorded one was judged by an older checker: a re-preview
    (:func:`re_preview_words`), never a fix. A drawing judged under the running report names it: a defect to fix."""
    from app.services.video_studio import quality

    if version is None or judged_before(version) is not None:
        return re_preview_words(version, scene_id)
    return (
        f"judged under the running quality report v{quality.QUALITY_REPORT_VERSION}: fix what it points at, then "
        f"preview it again (render_video_scene(scene='{scene_id}'))"
    )


def provenance_line(provenance: dict | None) -> str:
    """``checks v48 by build 1.261105.0 at 2026-11-05T09:00:00+00:00 (current)``: what a render's answer and
    ``get_video``'s list say of :func:`report_provenance`; ``""`` for none."""
    if not provenance:
        return ""
    from app.services.video_studio import quality

    version = provenance.get("quality_report_version")
    text = f"checks v{version}" if version is not None else "checks of an unknown quality report version"
    drawn_at = provenance.get("drawing_version")
    text += f" by build {provenance['build']}" if provenance.get("build") else " (build not recorded)"
    if provenance.get("checked_at"):
        text += f" at {provenance['checked_at']}"
    again = provenance.get("rejudged") or {}
    text += (
        " (current)"
        if provenance.get("current")
        else f" (older than the running checker, v{quality.QUALITY_REPORT_VERSION}: "
        + (
            f"re-judged under v{again['quality_report_version']} at {again.get('checked_at')}, nothing drawn, "
            f"{again.get('status')} with {again.get('findings', 0)} finding(s))"
            if again.get("current")
            else "get_video_jobs(render_ids=[...], rejudge=true) judges it anew, nothing drawn)"
        )
    )
    live = provenance.get("live_drawing_version")
    text += f", screen-insert drawing {drawn_at}" if drawn_at is not None else ", screen-insert drawing not recorded"
    if live is not None and not provenance.get("drawing_current"):
        # Unrecorded: the line already said so; "drawn at" names a recorded drawing only.
        older = f"drawn at drawing {drawn_at}, " if drawn_at is not None else ""
        text += f" ({older}live {live}: render again)"
    if provenance.get("recomputed_after_resume"):
        text += (
            f"; resumed across builds {', '.join(provenance['resumed_across_builds'])}, every frame judged again "
            "by the build that finished it"
        )
    return text


def findings_of(checks: dict | None) -> list[dict]:
    return [f for f in (checks or {}).get("findings") or [] if isinstance(f, dict)]


def harness_of(checks: dict | None) -> list[dict]:
    """What a render's harness drew wrong (:attr:`Checks.harness`): never among its findings."""
    return [f for f in (checks or {}).get("harness") or [] if isinstance(f, dict)]


def open_findings(checks: dict | None, *, same_quality: bool) -> list[dict]:
    """The findings a render left open that refuse another render of its revision.

    A failing finding (an audio one) refuses every render of the revision: it
    comes from the narration and the plan, which a quality does not change. A
    warning (a frozen picture) refuses only a render of the same quality - the
    same video again - so a draft that showed one never stops the final render
    the person chose to make anyway.
    """
    return [
        f
        for f in findings_of(checks)
        if not is_unexplained(f)
        and (
            (f.get("kind") in AUDIO_KINDS | PICTURE_KINDS and f.get("severity") == FAIL)
            or (same_quality and f.get("kind") in HELD_WARNINGS)
        )
    ]


def finding_line(finding: dict) -> str:
    where = f"{clock(finding.get('at'))}" + (f"-{clock(finding.get('end'))}" if finding.get("end") is not None else "")
    scene = f" scene `{finding['scene_id']}`" if finding.get("scene_id") else ""
    level = "FAIL" if finding.get("severity") == FAIL else "warn"
    frame = (finding.get("data") or {}).get("frame_file_id")
    shown = f" (frame: file `{frame}`)" if frame else ""
    return f"- [{level}] {where}{scene} ({finding.get('kind')}): {finding.get('message')}{shown}"


def insert_reports(checks: dict | None) -> list[dict]:
    """What a render measured of each screen insert (``checks.inserts``)."""
    return [r for r in (checks or {}).get("inserts") or [] if isinstance(r, dict)]


#: The 100 % crops an insert's report keeps beside its region's (:func:`insert_crop_ids`): the side where most of its
#: residual key sits, the worst stretch of its halo, of it drawn over what stands in front, of
#: its grey rim, of its source's strokes drawn across what stands in front.
INSERT_STRIPS = ("residual_crop", "halo_crop", "foreground_crop", "rim_crop", "through_crop")


def insert_crop_ids(checks: dict | None) -> list[str]:
    """The 100 % crops of the insert regions a render kept: their file ids, first, middle, last, per insert,
    then its :data:`INSERT_STRIPS` - the side where most of its residual key sits, the worst stretch of its halo, of
    it drawn over what stands in front, of its grey rim, of its source seen through what stands in front."""
    found = []
    for report in insert_reports(checks):
        found.extend(
            str(crop["file_id"]) for crop in report.get("crops") or [] if isinstance(crop, dict) and crop.get("file_id")
        )
        for name in INSERT_STRIPS:
            strip = report.get(name)
            if isinstance(strip, dict) and strip.get("file_id"):
                found.append(str(strip["file_id"]))
    return found


def finding_frame_ids(checks: dict | None) -> list[str]:
    """The frames a render's findings show (``data.frame_file_id``), and its harness's defects."""
    return [
        str(f["data"]["frame_file_id"])
        for f in [*findings_of(checks), *harness_of(checks)]
        if (f.get("data") or {}).get("frame_file_id")
    ]


def frame_file_ids(checks: dict | None) -> list[str]:
    """The frames a render's findings show (``data.frame_file_id``) and the crops of its screen inserts:
    files of the render, deleted with it."""
    return finding_frame_ids(checks) + insert_crop_ids(checks)


#: The most pictures a render's answer attaches: a render with several inserts and findings names dozens.
ATTACHED_EVIDENCE = 12


def evidence_files(checks: dict | None, lead: list[object]) -> tuple[list[str], list[str]]:
    """``(attached, linked)``: the files a render's answer shows, in order.

    *lead* (its frames, its poster) first, then for each insert the frame of
    its worst finding (a failing one before a warning, then the earliest) and
    its first, middle and last crops, then the rest - its residual side, the
    other findings' frames -, attached up to :data:`ATTACHED_EVIDENCE`; past
    it, linked: files of the conversation ``read_file_content`` opens, named
    by the answer. Every file the checks name is one or the other, once.
    """
    worst: dict[str, str] = {}
    for finding in sorted(findings_of(checks), key=lambda f: (f.get("severity") != FAIL, float(f.get("at") or 0.0))):
        data = finding.get("data") or {}
        if data.get("insert") and data.get("frame_file_id"):
            worst.setdefault(str(data["insert"]), str(data["frame_file_id"]))
    first = [str(fid) for fid in lead if fid]
    for report in insert_reports(checks):
        if str(report.get("key")) in worst:
            first.append(worst[str(report.get("key"))])
        first.extend(
            str(crop["file_id"]) for crop in report.get("crops") or [] if isinstance(crop, dict) and crop.get("file_id")
        )
    ordered = list(dict.fromkeys([*first, *insert_crop_ids(checks), *finding_frame_ids(checks)]))
    return ordered[:ATTACHED_EVIDENCE], ordered[ATTACHED_EVIDENCE:]


def with_files(checks: dict | None, copies: dict[str, str]) -> dict | None:
    """*checks* naming every file of theirs - its findings' frames, its inserts' crops - by the id *copies* maps it to.

    A render's own files are not the conversation's: the tool answer shows
    each through the conversation's copy (``builtin_tools.video_studio._file_refs``,
    its ``copies``), and the ids the answer gives must be those - the ones
    ``read_file_content`` opens. The stored checks are left as they are.
    """
    if not checks:
        return checks
    shown = json.loads(json.dumps(checks, default=str))
    for finding in [*findings_of(shown), *harness_of(shown)]:
        data = finding.get("data") or {}
        if data.get("frame_file_id"):
            data["frame_file_id"] = copies.get(str(data["frame_file_id"]), str(data["frame_file_id"]))
    for report in insert_reports(shown):
        strips = [report.get(name) for name in INSERT_STRIPS]
        for crop in [*(report.get("crops") or []), *(strip for strip in strips if isinstance(strip, dict))]:
            if isinstance(crop, dict) and crop.get("file_id"):
                crop["file_id"] = copies.get(str(crop["file_id"]), str(crop["file_id"]))
    return shown


def _size_text(size: Any) -> str | None:
    try:
        width, height = (int(v) for v in size)
    except (TypeError, ValueError):
        return None
    return f"{width}x{height}"


def insert_findings(checks: dict | None) -> dict[str, set[str]]:
    """``insert key -> the kinds of the findings`` a render's *checks* give it: what makes one of its
    measurements a finding (``insert_checks.classify``)."""
    found: dict[str, set[str]] = {}
    for finding in findings_of(checks):
        key = (finding.get("data") or {}).get("insert")
        if key:
            found.setdefault(str(key), set()).add(str(finding.get("kind")))
    return found


def insert_measured(report: dict, found: set[str] | frozenset[str] = frozenset()) -> list[Any]:
    """Every measurement of an insert's *report*, classed (``insert_checks.classify``), then the facts of its
    evidence: the resolution its numbers were measured on, its crops - each with the conversation's file id - and
    the box they were cut from."""
    from app.services.video_studio import insert_checks

    out = insert_checks.classify(report, found)
    measured = _size_text(report.get("measured_at"))
    if measured:
        out.append(insert_checks.Measured(insert_checks.FACT, f"measured on {measured} frames"))
    crops = [c for c in report.get("crops") or [] if isinstance(c, dict) and c.get("file_id")]
    if crops:
        shown = ", ".join(f"{c.get('word')} frame file `{c['file_id']}`" for c in crops)
        cropped = _size_text(report.get("cropped_at"))
        within = report.get("cropped_within")
        if isinstance(within, list | tuple) and len(within) == 2:
            # The first and last crops are the scene's own frames, never a blend with a neighbour.
            shown += (
                f" (cut from {clock(float(within[0]))} to {clock(float(within[1]))}: the first and last frames "
                "its scene owns alone, after its transition in and before its transition out)"
            )
        out.append(
            insert_checks.Measured(
                insert_checks.FACT,
                f"100% crops of the {cropped} frame: {shown}"
                if cropped
                else f"100% crops at the render's resolution: {shown}",
            )
        )
        box = next((c.get("box") for c in crops if c.get("box")), None)
        if box:
            out.append(
                insert_checks.Measured(
                    insert_checks.FACT,
                    f"its region box {json.dumps(box)} (save_video_frame region: {{box}} cuts any other moment at 100%)",
                )
            )
    return out


def insert_heading(report: dict) -> str:
    """``Insert Scene 3: a screen insert (refined quad [...])``: what an insert's measurements are said under."""
    quad = report.get("refined")
    where = f" (refined quad {json.dumps(quad)})" if quad else ""
    if report.get("auto_quad"):
        where = f" (quad auto: the bezel found on its first footage frame, {json.dumps(report['auto_quad'])})"
    beside = ", a neighbour's, measured on this preview's frames of the join" if report.get("neighbour") else ""
    return f"Insert {report.get('label') or report.get('key')}{where}{beside}"


def insert_lines(checks: dict | None) -> list[str]:
    """The ``Insert`` lines of a tool answer: what each screen insert's compositing measured, and its crops,
    each measurement with its class first (``finding``, ``by design``, ``fact``).

    A residual key said by where it sits, with the crop of the side holding
    most of it; its colours at its light
    (``insert_checks.colour_numbers``) - what the ``light`` is tuned by - and
    the resolution the numbers were measured on and the crops cut from: a
    draft's for a preview, the render's own for a render (2160p for an ultra
    one).
    """
    found = insert_findings(checks)
    lines = []
    for report in insert_reports(checks):
        parts = [item.line() for item in insert_measured(report, found.get(str(report.get("key")), set()))]
        lines.append(f"{insert_heading(report)}: {'; '.join(parts)} over {int(report.get('frames') or 0)} frames.")
    return lines


def _heard(forms: list | None) -> str:
    return f" (heard {', '.join(repr(h) for h in (forms or [])[:3])})" if forms else ""


def _term_said(term: dict) -> str:
    """``OpenAEV 3 of 3 in the takes kept (heard 'OpenAEV'), 1 attempt failed on scene st-demo (heard 'OpenA')``: a
    term of the final voice, and each take it failed in that the render does not play."""
    failed = [item for item in term.get("attempts_failed") or [] if isinstance(item, dict)]
    said = f"{term.get('term')} {term.get('passed', 0)} of {term.get('occurrences', 0)}"
    if failed:
        said = f"{said} in the takes kept" if term.get("occurrences") else f"{term.get('term')} in no take kept"
    said += _heard(term.get("heard"))
    for item in failed:
        times = int(item.get("times") or 0)
        said += f", {times} attempt{'s' if times != 1 else ''} failed on scene {item.get('scene_id')}{_heard(item.get('heard'))}"
    return said


def pronunciation_lines(section: dict) -> list[str]:
    """The ``Pronunciation`` lines of a tool answer: each lexicon term of the final voice, its occurrences and hearings,
    and every take that failed on it the render does not play."""
    terms = [t for t in section.get("terms") or [] if isinstance(t, dict)]
    if not section.get("status"):
        return []
    reason = f" ({section['reason']})" if section.get("reason") else ""
    if not terms:
        return [f"Pronunciation: {section['status']}{reason}."]
    return [f"Pronunciation: {section['status']}{reason}: {', '.join(_term_said(t) for t in terms[:12])}."]


def format_checks(checks: dict | None) -> str:
    """The ``Checks`` section of a tool answer: the outcome and every finding with its time; empty with none run."""
    if not checks:
        return ""
    lines = [f"Checks: {checks.get('status')}.", *check_lines(checks)]
    lines.extend(insert_lines(checks))
    found = findings_of(checks)
    lines.extend(finding_line(f) for f in found[:LISTED_FINDINGS])
    if len(found) > LISTED_FINDINGS:
        lines.append(f"- ... and {len(found) - LISTED_FINDINGS} more findings.")
    lines.extend(f"Note: {note}" for note in (checks.get("notes") or [])[:5])
    return "\n".join(lines)


def check_lines(checks: dict | None) -> list[str]:
    """What a render's checks heard and watched, check by check: its audio, music, picture, pronunciation and
    narrations - never a finding of theirs, listed apart; facts of a render's answer (``render_answer``)."""
    if not checks:
        return []
    lines: list[str] = []
    audio, frames = checks.get("audio") or {}, checks.get("frames") or {}
    if audio.get("status"):
        heard = (
            f", {audio['wer']:.0%} of the narration's words heard wrong"
            if isinstance(audio.get("wer"), int | float)
            else ""
        )
        # Who heard it: the narrations' audio goes to that provider, whatever speaks them.
        if audio.get("model") and audio.get("provider"):
            heard += f" (speech-to-text: {audio['model']} on {audio['provider']})"
        lines.append(
            f"Audio: {audio['status']}{heard}" + (f" ({audio['reason']})" if audio.get("reason") else "") + "."
        )
    if isinstance(audio.get("music"), dict):
        from app.services.video_studio.music_level import summary_line

        lines.append(summary_line(audio["music"]))
    if isinstance(audio.get("mix"), dict):
        from app.services.video_studio import mix_check

        lines.append(mix_check.summary_line(audio["mix"]))
    if frames.get("status"):
        line = f"Picture: {frames['status']}" + (f" ({frames['reason']})" if frames.get("reason") else "")
        if frames.get("resumed_at"):
            line += f"; watched on from {clock(frames['resumed_at'])} after a resume, what it judged before kept"
        # A window the watch could not judge in its time: where, and why, so the content is told from the worker.
        if unjudged := [item for item in frames.get("unjudged") or [] if isinstance(item, dict)]:
            shown = "; ".join(
                f"{clock(item.get('at'))} to {clock(item.get('end'))}"
                + (f" in {', '.join(f'`{scene}`' for scene in item['scenes'])}" if item.get("scenes") else "")
                + (f" ({item['why']})" if item.get("why") else "")
                for item in unjudged[:3]
            )
            more = f" and {len(unjudged) - 3} more" if len(unjudged) > 3 else ""
            line += f"; {len(unjudged)} window{'s' if len(unjudged) != 1 else ''} not judged: {shown}{more}"
        lines.append(line + ".")
    if isinstance(checks.get("cuts"), dict):
        from app.services.video_studio.cut_blend import summary_line as cuts_line

        if said := cuts_line(checks["cuts"]):
            lines.append(said)
    lines.extend(pronunciation_lines(checks.get("pronunciation") or {}))
    voices = checks.get("voice") or []
    if voices:
        failed = [v for v in voices if v.get("status") == FAILED]
        fallback = [v for v in voices if v.get("fallback") and v.get("status") != FAILED]
        unchecked = [v for v in voices if v.get("status") == "unchecked" and not v.get("fallback")]
        passed = len(voices) - len(failed) - len(fallback) - len(unchecked)
        text = f"Narrations heard back: {passed} of {len(voices)} passed in the video's voice"
        if fallback:
            # The closest voice of its speech model; a render made before said the platform's default voice.
            text += f", {len(fallback)} spoken by another voice after the video's failed (each listed below)"
        if unchecked:
            text += f", {len(unchecked)} not checked"
        lines.append(text + ".")
    return lines


def failure_summary(checks: Checks, *, drawn: bool = True) -> str:
    """The error of a whole render its checks failed: the first findings with their times.

    *drawn* ``False``: stopped before its frames were drawn (a narration that failed every attempt).
    """
    failing = [f for f in sorted(checks.findings, key=lambda f: f.at) if f.severity == FAIL]
    shown = "; ".join(f"{clock(f.at)}{f' scene {f.scene_id}' if f.scene_id else ''}: {f.message}" for f in failing[:3])
    more = f" (and {len(failing) - 3} more)" if len(failing) > 3 else ""
    kept = " Its MP4 is kept to look at." if drawn else " It stopped before drawing its frames."
    picture = any(f.kind in PICTURE_KINDS for f in failing)
    audio = any(f.kind not in PICTURE_KINDS for f in failing)
    check = "audio and picture checks" if picture and audio else "picture check" if picture else "audio check"
    return (
        f"The render failed its {check}: {shown}{more}.{kept} Fix the narration or the scenes and render "
        "again; render_video(force: true) renders anyway."
    )[:2000]


# ── The gate ──────────────────────────────────────────────────────────────


#: The top of the spec every scene's picture depends on.
_LOOK_KEYS = ("format", "theme", "captions")
#: The top of the spec every scene's sound depends on: the music under it. The voice-over is heard by the
#: narration check, never by drawing a scene again.
_AUDIO_KEYS = ("music",)
#: What a scene's content digest leaves out of the look: the theme every scene wears alike. With the music,
#: an edit of these alone keeps a scene's passing drawing (:func:`drawing_of`).
_SHARED_LOOK_KEYS = ("theme",)
#: What :func:`scene_digests` hashes, by version: a drawing recorded under another one is no drawing of the scene
#: as it is read now - neither changed nor current, as if never drawn.
DIGEST_VERSION = 4
#: The drawings a video keeps of each scene, by digest, newest first.
DRAWINGS_KEPT = 8
#: What a scene's preview never draws of a neighbour: its narration and the pause before it - heard in the
#: neighbour's own preview and the narration check -, and its own transition in, which the window replaces
#: with the transition it plans between the two (:func:`scene_digests`).
_UNDRAWN_IN_A_NEIGHBOUR = frozenset({"narration", "narration_delay", "transition"})


def _media_part(kind: str, ref: str, row: Any) -> str:
    """What a scene plays of one medium: a deck's revision, the take a shot, a recording or an AI track plays.

    The take that plays, never the job's status: a shot generated again in
    place keeps playing its take - nothing changed on screen - until a new
    take replaces it, and an attempt that fails beside it changes nothing.
    """
    from app.models.video_studio import JOB_READY
    from app.services.video_studio import clips

    if row is None:
        return f"{kind}:{ref}:gone"
    if kind == "presentation":
        return f"{kind}:{ref}:{getattr(row, 'revision', '')}"
    if kind == "clip":
        plays = clips.playable(row)
    else:
        plays = row.status == JOB_READY and bool(row.file_id) and getattr(row, "expired_at", None) is None
    return f"{kind}:{ref}:{row.file_id}" if plays else f"{kind}:{ref}:none"


def _drawn_part(kind: str, ref: str, row: Any) -> str:
    """What a scene draws of one medium: a deck's slides as an audience sees them - without their speaker
    notes - with the template and the scheme it wears; any other medium as :func:`_media_part` says it."""
    if kind != "presentation" or row is None:
        return _media_part(kind, ref, row)
    from app.services.presentation_public_links import public_spec

    shown = {
        "spec": public_spec(getattr(row, "spec", None)),
        "theme": getattr(row, "theme", None),
        "template": getattr(row, "template_container_id", None),
    }
    return f"{kind}:{ref}:{hashlib.sha256(json.dumps(shown, sort_keys=True, default=str).encode('utf-8')).hexdigest()[:24]}"


@dataclass(frozen=True)
class SceneDigests:
    """What :func:`digest_scenes` hashes of every scene: all its drawing depends on, and its own content."""

    #: ``scene id -> digest`` (:func:`scene_digests`): what a drawing is recorded under and matched by.
    whole: dict[str, str]
    #: ``scene id -> digest`` of the same without the video's theme and music: a drawing that passed keeps
    #: counting after an edit of only those (:func:`drawing_of`).
    content: dict[str, str]
    #: ``scene id -> digest`` of what a whole render draws of it: its frames' inputs and nothing heard - a
    #: render whose scenes all keep it draws the same frames, whatever else the revision changed
    #: (``projects.carry_renders``).
    drawn: dict[str, str] = field(default_factory=dict)
    #: The digest of what a whole render plays beside its frames: the voice-over's level and the lexicon it is
    #: said with, each scene's own sound, the music and the track it plays. With :attr:`drawn`, all a whole render is
    #: made of: a ready render whose both match the revision's covers it (``projects.covering_render``).
    heard: str = ""
    #: ``scene id -> digest`` of the lexicon entries each narration's voice says: what a scene playing a
    #: fallback take is re-checked by when it moves (:func:`fallback_digest`).
    said: dict[str, str] = field(default_factory=dict)
    #: :attr:`heard` less the lexicon: the voice-over's level, each scene's own sound and the music.
    beside: str = ""
    #: :attr:`drawn` less the voice-over's pronunciations: what the frames are drawn from but for the lengths
    #: the takes give the narrations - a re-checked take is held to the very frames it draws (``voice_recheck``).
    picture: dict[str, str] = field(default_factory=dict)
    #: ``scene id -> {digest recorded by an earlier release: the digest it is now}`` (:func:`adopted`): :attr:`whole` and
    #: :attr:`content` hashed as before - every default the spec fills -, with and without what draws nothing
    #: (``spec.UNDRAWN_SCENE_FIELDS``). A drawing an earlier release recorded of the scene as it is now still counts.
    aliases: dict[str, dict[str, str]] = field(default_factory=dict)
    #: ``scene id -> {part: fingerprint}`` of what the scene is drawn from (:func:`changed_parts`): its own
    #: fields, each overlay's, its media, its transitions as planned, its neighbours' edges, the look and the music -
    #: what the refusal of a changed scene names.
    parts: dict[str, dict[str, str]] = field(default_factory=dict)


def fallback_digest(said: str) -> str:
    """The reuse digest of a scene a render plays with a fallback take: what its narration is said with
    (:attr:`SceneDigests.said`) and the voice pipeline (``voice_check.pipeline_version``). Either moved, the take may
    now pass at the video's speed: a ready render playing it no longer covers the scene unasked (``voice_recheck``)."""
    from app.services.video_studio import voice_check

    return hashlib.sha256(f"fallback|{voice_check.pipeline_version()}|{said}".encode()).hexdigest()[:40]


#: How :func:`stale_release` names each part of :func:`release_drawing`.
_RELEASE_PARTS = {"drawing": "screen-insert drawing version", "insert_build": "insert build version"}


def release_drawing() -> dict[str, int]:
    """How this release draws a whole render's frames: its screen inserts' drawing
    (``screen_insert.DRAWING_VERSION``) and their build (``insert_builds.BUILD_VERSION``), each moved whenever what it
    draws changes. Recorded on every whole render (``render.drawing_of``): one drawn by another release covers no
    revision (``projects.covering_render``), whatever its scenes' digests say - a matte fixed changes every insert
    frame and no scene's spec."""
    from app.services.video_studio import insert_builds, screen_insert

    return {"drawing": screen_insert.DRAWING_VERSION, "insert_build": insert_builds.BUILD_VERSION}


def stale_release(drawing: dict | None, checks: dict | None = None) -> str | None:
    """Why a render whose ``VideoRender.drawing`` is *drawing* and whose stored checks are *checks* was drawn or
    judged by another release than this one: ``drawn at screen-insert drawing version 7, now 8`` (:func:`release_drawing`),
    ``judged under quality report v48, now v49`` (``quality.QUALITY_REPORT_VERSION``: a fix that changes how whole-render
    frames are drawn moves it with the gate that reads its defect, like a cut placed on a whole frame and ``cut_blend``),
    or both - ``unrecorded`` for a render made before renders recorded it; ``None`` when it is this release's.
    *checks* ``None`` (not given) leaves the quality report out of the comparison."""
    from app.services.video_studio import quality

    then = drawing.get("release") if isinstance(drawing, dict) else None
    then = then if isinstance(then, dict) else {}
    changed = [
        f"{_RELEASE_PARTS[part]} {then[part] if then.get(part) is not None else 'unrecorded'}, now {now}"
        for part, now in release_drawing().items()
        if then.get(part) != now
    ]
    judged = _int_or_none(checks.get("quality_report_version")) if isinstance(checks, dict) else None
    running = quality.QUALITY_REPORT_VERSION
    said = [f"drawn at {'; '.join(changed)}"] if changed else []
    if checks is not None and (judged is None or judged < running):
        said.append(
            f"judged under quality report {f'v{judged}' if judged is not None else 'version unrecorded'}, now v{running}"
        )
    return ", and ".join(said) if said else None


def release_line(drawing: dict | None) -> str:
    """``drawn at screen-insert drawing version 19, insert build version 9 (current)``: what ``get_video``'s render
    list says of the release a whole render recorded (:func:`release_drawing`); ``""`` for a render that recorded
    none (a scene preview, a render made before renders recorded it)."""
    then = drawing.get("release") if isinstance(drawing, dict) else None
    if not isinstance(then, dict) or not then:
        return ""
    now = release_drawing()
    parts = ", ".join(
        f"{_RELEASE_PARTS[part]} {then[part] if then.get(part) is not None else 'unrecorded'}" for part in now
    )
    older = [f"{_RELEASE_PARTS[part]} {value}" for part, value in now.items() if then.get(part) != value]
    return f"drawn at {parts} " + (f"(older than this release: {', '.join(older)})" if older else "(current)")


def sound_sources(effective: dict) -> str:
    """What a render gathers the music from before it draws a frame, digested: whether music plays and its
    file - an AI track, an upload. A render carried to a later revision mixes that revision's sound from what it
    gathered (``render.run_render``): it is carried only while this is the same."""
    music = effective.get("music") if isinstance(effective.get("music"), dict) else {}
    picked = {key: music.get(key) for key in ("enabled", "music_id", "file_id")}
    return hashlib.sha256(json.dumps(picked, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:24]


async def scene_digests(db: Any, effective: dict, lexicon: Any = None) -> dict[str, str]:
    """``scene id -> digest`` of every scene of the *effective* spec (:func:`digest_scenes`'s ``whole``)."""
    return (await digest_scenes(db, effective, lexicon)).whole


async def digest_scenes(db: Any, effective: dict, lexicon: Any = None) -> SceneDigests:
    """The digests of every scene of the *effective* spec: what it shows, beside its neighbours, over the music.

    A preview draws a scene with the one before and the one after it, its
    transitions in and out decided by them (``timeline.auto_transition``), over
    the video's music. So the digest of a scene hashes what it shows -
    its spec as resolved (its transition in, the video's
    ``default_transition`` when it names none; a clip's own sound, as
    ``clip_sound`` decides it; the video's fade in or out on its first or last
    scene), the caption text a lexicon entry applied to the captions writes
    (*lexicon*: the video's, the platform's entries under its own; ``None``:
    its own alone), the take it plays -, what its preview draws of each
    neighbour - the neighbour's picture at the edge (its resolved spec but its
    narration and its own transition in, :data:`_UNDRAWN_IN_A_NEIGHBOUR`, and
    the take it plays) and the transition between the two as the window plans
    it (``projects.preview_transitions``) -, the video's look and its
    music. Editing a scene's picture changes its own digest and its
    neighbours' - three at most -, never another scene's; editing its
    narration changes its own, and a neighbour's only where it changes the
    transition between them; a shared input changes the scenes that read it.
    The same content gives the same digest whatever the revision, so an edit
    reverted gives back the digest it had. A recording recorded again, a new
    take of a shot or a deck edited is a change of the scenes around it; a
    shot being generated again while its take still plays is none. The
    voice-over is not part of it: a voice, a style or a lexicon
    reading changed is heard again by the narration check every whole render
    runs before it draws a frame (``voice_check.speak_checked``), never by
    drawing the scene again. A preview and a whole render record it
    (:func:`record_seen`); the gate compares it with what they recorded.

    The ``content`` digest hashes the same less the video's theme
    and its music: a watermark moved, a backdrop changed or a new track
    leaves every scene's content as it was, and a scene whose drawing passed
    keeps it (:func:`drawing_of`) instead of being previewed again.

    The ``drawn`` digest hashes what a whole render's frames of the
    scene are drawn from and nothing heard: the same as ``whole`` without a
    clip's own sound and level (``spec.HEARD_SCENE_FIELDS``) nor the music -
    unless the picture follows its beats (``theme.motion.beat_sync``) -, with
    the voice-over but its level (it times the narrations and their
    captions), and a deck by its slides as an audience sees them, without
    their speaker notes.

    The ``heard`` digest hashes, for the whole video, what a whole
    render plays beside those frames: the voice-over's level, the lexicon
    its narrations are said with (the platform's entries included), each
    scene's own sound (``spec.HEARD_SCENE_FIELDS`` and the clip's sound as
    ``clip_sound`` decides it) and the music with the take it plays.

    What a voice change alone moves, apart: ``said``, each
    narrated scene's lexicon entries as its voice says them; ``beside``, the
    heard digest less the lexicon; ``picture``, each drawn digest less the
    voice-over's pronunciations (they draw nothing but the narrations'
    lengths, which a re-checked take is held to by the frames it draws). A
    ready render playing a fallback take is offered a re-check of its scene
    when only these moved (``voice_recheck.offered``).
    """
    from app.core.utils import safe_uuid
    from app.models.presentation import Presentation
    from app.models.video_studio import VideoClip, VideoMusic, VideoTake
    from app.services.video_studio import lexicon as lexicons
    from app.services.video_studio.projects import preview_transitions
    from app.services.video_studio.spec import (
        HEARD_SCENE_FIELDS,
        HEARD_VOICEOVER_FIELDS,
        UNDRAWN_INSERT_FIELDS,
        UNDRAWN_SCENE_FIELDS,
        clip_sound_choice,
        parse_video_spec,
        referenced_ids,
    )

    if lexicon is None:
        lexicon = lexicons.merge((), (effective.get("voiceover") or {}).get("pronunciations") or ())
    captioned = bool((effective.get("captions") or {}).get("enabled"))
    parsed = parse_video_spec(effective)
    clips_of = {scene.id: scene for scene in parsed.scenes if scene.type == "clip"}
    models_of = {scene.id: scene for scene in parsed.scenes if scene.id}
    models = {"take": VideoTake, "clip": VideoClip, "presentation": Presentation, "music": VideoMusic}
    rows: dict[tuple[str, str], Any] = {}

    async def played(part: dict, *, drawn: bool = False) -> list[str]:
        refs = referenced_ids(part)
        found = []
        for kind, model in models.items():
            for ref in sorted(refs[kind]):
                if (kind, ref) not in rows:
                    uid = safe_uuid(str(ref))
                    rows[(kind, ref)] = await db.get(model, uid) if uid is not None else None
                found.append((_drawn_part if drawn else _media_part)(kind, str(ref), rows[(kind, ref)]))
        return found

    def dumped(value: object) -> str:
        return json.dumps(value, sort_keys=True, default=str)

    scenes = [scene for scene in effective.get("scenes") or [] if isinstance(scene, dict) and scene.get("id")]

    def resolved(index: int, scene: dict, default_transition: object = None) -> dict:
        """*scene* with what it reads of the video: its transition in, its own sound, the video's fades; its
        overlays as drawn (``spec.UNDRAWN_INSERT_FIELDS`` left out)."""
        fallback = effective.get("default_transition") if default_transition is None else default_transition
        out = {**scene, "transition": scene.get("transition") or fallback or {}}
        if isinstance(scene.get("overlays"), list):
            out["overlays"] = [
                {k: v for k, v in overlay.items() if k not in UNDRAWN_INSERT_FIELDS}
                if isinstance(overlay, dict)
                else overlay
                for overlay in scene["overlays"]
            ]
        if (clip := clips_of.get(scene["id"])) is not None:
            out["sound"] = clip_sound_choice(clip, parsed)[0]
        if index == 0:
            out["fade_in"] = bool(effective.get("fade_in", True))
        if index == len(scenes) - 1:
            out["fade_out"] = bool(effective.get("fade_out", True))
        return out

    def shown(scene: dict, media: list[str]) -> str:
        parts = [dumped(scene)]
        narration = scene.get("narration")
        caption = lexicon.caption_text(narration) if narration and captioned else narration
        if caption != narration:
            parts.append(f"captions:{caption}")
        return "|".join([*parts, *media])

    def edge(scene: dict, media: list[str]) -> str:
        return "|".join([dumped({k: v for k, v in scene.items() if k not in _UNDRAWN_IN_A_NEIGHBOUR}), *media])

    sound = {key: effective.get(key) for key in _AUDIO_KEYS}
    music_played = await played({"scenes": [], "music": sound.get("music") or {}})
    media = [await played({"scenes": [scene]}) for scene in scenes]
    resolved_scenes = [resolved(index, scene) for index, scene in enumerate(scenes)]
    transitions = await preview_transitions(db, effective)

    def digest(parts: list[str]) -> str:
        return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:40]

    def hashed(shapes: list[dict], look: str, own_look: str, heard: str, tag: str = "") -> tuple[dict, dict]:
        """``(whole, content)`` of the scenes resolved as *shapes*, over *look* (``own_look`` without the theme)
        and the music *heard*: what a preview draws of each, beside its neighbours."""
        own = [shown(scene, played_by) for scene, played_by in zip(shapes, media, strict=True)]
        edges = [edge(scene, played_by) for scene, played_by in zip(shapes, media, strict=True)]
        whole_of: dict[str, str] = {}
        content_of: dict[str, str] = {}
        for index, scene in enumerate(scenes):
            into, out_of = transitions.get(str(scene["id"]), (None, None))
            parts = [
                f"before:{edges[index - 1] if index > 0 else 'start'}",
                f"in:{dumped(into)}",
                f"scene:{own[index]}",
                f"out:{dumped(out_of)}",
                f"after:{edges[index + 1] if index + 1 < len(edges) else 'end'}",
            ]
            whole_of[str(scene["id"])] = digest([f"{tag}v{DIGEST_VERSION}", look, heard, *music_played, *parts])
            content_of[str(scene["id"])] = digest([f"{tag}content-v{DIGEST_VERSION}", own_look, *parts])
        return whole_of, content_of

    # As hashed by an earlier release - every default the spec fills - and the same without what draws nothing: what the
    # drawings an earlier release recorded are matched by (``SceneDigests.aliases``).
    full_look = dumped({key: effective.get(key) for key in _LOOK_KEYS})
    full_own_look = dumped({k: effective.get(k) for k in _LOOK_KEYS if k not in _SHARED_LOOK_KEYS})
    legacy_whole, legacy_content = hashed(resolved_scenes, full_look, full_own_look, dumped(sound))
    undrawn = [{k: v for k, v in scene.items() if k not in UNDRAWN_SCENE_FIELDS} for scene in resolved_scenes]
    bare_whole, bare_content = hashed(undrawn, full_look, full_own_look, dumped(sound))

    # What a drawing is recorded under: each scene as it renders - its fields set to anything but their
    # default, what draws nothing left out (``spec.UNDRAWN_SCENE_FIELDS``) -, the look and the music likewise. A
    # release that adds a field with a default, or an edit of a field that draws nothing, moves no scene.
    def settled(value: object) -> object:
        return value.model_dump(mode="json", exclude_defaults=True) if hasattr(value, "model_dump") else value

    def settled_scene(scene: dict) -> dict:
        model = models_of.get(scene["id"])
        if model is None:
            return {k: v for k, v in scene.items() if k not in UNDRAWN_SCENE_FIELDS}
        out = {k: v for k, v in settled(model).items() if k not in UNDRAWN_SCENE_FIELDS}
        out["id"], out["type"] = scene["id"], scene.get("type")
        if model.transition is not None:
            # Its own transition, even one of every default: never the video's default_transition in its place.
            out["transition"] = settled(model.transition) or _ALL_DEFAULTS
        return out

    settled_default = settled(parsed.default_transition) or _ALL_DEFAULTS
    settled_scenes = [resolved(index, settled_scene(scene), settled_default) for index, scene in enumerate(scenes)]
    settled_look = {key: settled(getattr(parsed, key, None)) for key in _LOOK_KEYS}
    settled_sound = {key: settled(getattr(parsed, key, None)) for key in _AUDIO_KEYS}
    whole, content = hashed(
        settled_scenes,
        dumped(settled_look),
        dumped({k: v for k, v in settled_look.items() if k not in _SHARED_LOOK_KEYS}),
        dumped(settled_sound),
        tag="settled-",
    )
    aliases = {
        sid: {
            **{form[sid]: whole[sid] for form in (legacy_whole, bare_whole)},
            **{form[sid]: content[sid] for form in (legacy_content, bare_content)},
        }
        for sid in whole
    }
    parts_of = {
        str(scene["id"]): _fingerprint(
            shape,
            media[index],
            transitions.get(str(scene["id"]), (None, None)),
            settled_scenes[index - 1] if index > 0 else None,
            settled_scenes[index + 1] if index + 1 < len(settled_scenes) else None,
            lexicon.caption_text(scene.get("narration")) if scene.get("narration") and captioned else None,
            settled_look,
            [dumped(settled_sound), *music_played],
        )
        for index, (scene, shape) in enumerate(zip(scenes, settled_scenes, strict=True))
    }

    # What a whole render draws: nothing heard, the voice-over that times the narrations, the music only under
    # a picture that follows its beats, a deck without its speaker notes.
    motion = getattr(parsed.theme, "motion", None)
    beats = getattr(motion, "beat_sync", "off") not in (None, "off")
    spoken = {k: v for k, v in (effective.get("voiceover") or {}).items() if k not in HEARD_VOICEOVER_FIELDS}
    look = dumped({key: effective.get(key) for key in _LOOK_KEYS})
    on_beats = (
        [dumped(sound), *await played({"scenes": [], "music": sound.get("music") or {}}, drawn=True)] if beats else []
    )
    drawn_common = [f"drawn-v{DIGEST_VERSION}", look, dumped(spoken), *on_beats]
    unheard = [
        {k: v for k, v in scene.items() if k not in HEARD_SCENE_FIELDS and k != "sound"} for scene in resolved_scenes
    ]
    drawn_media = [await played({"scenes": [scene]}, drawn=True) for scene in scenes]
    drawn_own = [shown(scene, played_by) for scene, played_by in zip(unheard, drawn_media, strict=True)]
    drawn_edges = [edge(scene, played_by) for scene, played_by in zip(unheard, drawn_media, strict=True)]
    # What a whole render plays beside its frames: what the drawn digests leave out of the sound.
    level = dumped({k: v for k, v in (effective.get("voiceover") or {}).items() if k in HEARD_VOICEOVER_FIELDS})
    own_sounds = [
        f"{scene['id']}:{dumped({k: scene.get(k) for k in (*sorted(HEARD_SCENE_FIELDS), 'sound')})}"
        for scene in resolved_scenes
    ]
    heard = digest(
        [f"heard-v{DIGEST_VERSION}", level, f"lexicon:{lexicon.digest()}", dumped(sound), *music_played, *own_sounds]
    )
    # The same less the lexicon, and the entries each narration's voice says: a lexicon edit moves the scenes
    # whose narration writes the term, never another.
    beside = digest([f"beside-v{DIGEST_VERSION}", level, dumped(sound), *music_played, *own_sounds])
    voiced = lexicon.voice()
    said = {
        str(scene["id"]): digest(
            [f"said-v{DIGEST_VERSION}", dumped([entry.as_dict() for entry in voiced.present(scene["narration"])])]
        )
        for scene in scenes
        if scene.get("narration")
    }
    # What the frames are drawn from but for the lengths the takes give the narrations: the pronunciations
    # left out of the voice-over, a caption they rewrite still in its scene.
    picture_common = [
        f"picture-v{DIGEST_VERSION}",
        look,
        dumped({k: v for k, v in spoken.items() if k != "pronunciations"}),
        *on_beats,
    ]

    drawn: dict[str, str] = {}
    picture: dict[str, str] = {}
    for index, scene in enumerate(scenes):
        into, out_of = transitions.get(str(scene["id"]), (None, None))
        drawn_parts = [
            f"before:{drawn_edges[index - 1] if index > 0 else 'start'}",
            f"in:{dumped(into)}",
            f"scene:{drawn_own[index]}",
            f"out:{dumped(out_of)}",
            f"after:{drawn_edges[index + 1] if index + 1 < len(drawn_edges) else 'end'}",
        ]
        drawn[str(scene["id"])] = digest([*drawn_common, *drawn_parts])
        picture[str(scene["id"])] = digest([*picture_common, *drawn_parts])
    return SceneDigests(whole, content, drawn, heard, said, beside, picture, aliases, parts_of)


#: A settled value of every default (``model_dump(exclude_defaults=True)`` gives ``{}``), told from no value at all.
_ALL_DEFAULTS = {"defaults": True}
#: How :func:`changed_parts` names the parts of a scene :func:`_fingerprint` reads that are not one of its fields.
_PART_NAMES = {"transition": "transition in"}
#: The hexadecimal digits of a part's fingerprint (:func:`_fingerprint`): kept on the scene's newest drawing alone.
PART_DIGITS = 10
#: The most parts a refusal or a save names of one changed scene (:func:`changed_parts`).
PARTS_NAMED = 4


def _fingerprint(
    scene: dict,
    media: list[str],
    planned: tuple,
    before: dict | None,
    after: dict | None,
    caption: str | None,
    look: dict,
    heard: list[str],
) -> dict[str, str]:
    """``{part: fingerprint}`` of what a scene resolved as it renders (*scene*, :func:`digest_scenes`) is drawn from:
    each of its own fields, each field of each overlay (``overlay 0 quad``), the media it plays, its
    transitions in and out as its preview plans them, what it draws of its neighbours, the caption a lexicon entry
    writes, the look and the music. :data:`PART_DIGITS` hexadecimal digits a part: what a refusal names, never what
    matches."""

    def mark(value: object) -> str:
        return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:PART_DIGITS]

    def edge(neighbour: dict | None) -> str:
        return mark({k: v for k, v in neighbour.items() if k not in _UNDRAWN_IN_A_NEIGHBOUR} if neighbour else None)

    out: dict[str, str] = {}
    for key, value in scene.items():
        if key in ("id", "overlays"):
            continue
        out[_PART_NAMES.get(key, key)] = mark(value)
    for index, overlay in enumerate(scene.get("overlays") or []):
        out[f"overlay {index}"] = mark(sorted(overlay) if isinstance(overlay, dict) else overlay)
        for key, value in overlay.items() if isinstance(overlay, dict) else ():
            out[f"overlay {index} {key}"] = mark(value)
    into, out_of = planned if isinstance(planned, tuple) and len(planned) == 2 else (None, None)
    out.update(
        {
            "media": mark(media),
            "planned transition in": mark(into),
            "transition out": mark(out_of),
            "scene before": edge(before),
            "scene after": edge(after),
            "look": mark({k: v for k, v in look.items() if k not in _SHARED_LOOK_KEYS}),
            "theme": mark({k: v for k, v in look.items() if k in _SHARED_LOOK_KEYS}),
            "music": mark(heard),
        }
    )
    if caption is not None and caption != scene.get("narration"):
        out["captions"] = mark(caption)
    return out


def scene_passed(checks: dict | None, scene_id: str) -> bool:
    """Whether a render's *checks* left *scene_id* clean: no failing finding and no frozen picture in it (or unplaced) -
    a harness defect stored among the findings by an earlier release (:func:`is_unexplained`) is none."""
    return not any(
        f.get("scene_id") in (scene_id, None)
        and (f.get("severity") == FAIL or f.get("kind") in HELD_WARNINGS)
        and not is_unexplained(f)
        for f in findings_of(checks)
    )


def _drawings(entry: object) -> list[dict]:
    """The drawings an entry of ``VideoProject.scene_previews`` keeps, newest first; none for another version."""
    if not isinstance(entry, dict) or entry.get("version") != DIGEST_VERSION:
        return []
    kept = entry.get("drawings")
    return [d for d in kept if isinstance(d, dict) and d.get("digest")] if isinstance(kept, list) else []


def record_seen(
    previews: dict | None,
    seen: dict[str, str],
    render_id: object,
    *,
    checks: dict | None = None,
    content: dict[str, str] | None = None,
    revision: int | None = None,
    stage: str | None = None,
    joined: dict[str, str] | None = None,
    parts: dict[str, dict[str, str]] | None = None,
    fps: float | None = None,
) -> dict:
    """*previews* (``VideoProject.scene_previews``) with the scenes *seen* by the render *render_id* recorded.

    Each drawing says whether its *checks* passed on that scene (:func:`scene_passed`), the *revision* it
    drew (what a finding it clears is cleared by), its *content* digest when given
    (:class:`SceneDigests`), what its inserts measured at their settings and whether each of their gates passed
    (:func:`insert_measures`), the findings nothing in the spec causes it left
    (:func:`unexplained_findings`), what the presence gate read of each insert on the scene's joins, given the digest
    every scene was drawn at (*joined*, :func:`join_measures`), and its *stage* - ``preview`` or ``render``
    (what a later finding that disagrees with it names). The newest drawing alone keeps the fingerprint of each part
    the scene is drawn from (*parts*, :attr:`SceneDigests.parts`): what the refusal of a scene changed since
    names (:func:`changed_parts`). Its frame rate (*fps*): the frames its gates judged, what a later drawing at
    another rate that disagrees with it says (``insert_checks.disagreement_cause``).
    An entry keeps the scene's last :data:`DRAWINGS_KEPT` drawings by digest, newest first - a digest drawn
    again replaces its older drawing - with the newest one's fields and the :data:`DIGEST_VERSION` they
    were hashed under at its top.
    """
    from datetime import UTC, datetime

    now = datetime.now(UTC).isoformat()
    kept = dict(previews) if isinstance(previews, dict) else {}
    for scene_id, digest in seen.items():
        if not digest:
            continue
        drawing = {"digest": digest, "render_id": str(render_id), "at": now, "passed": scene_passed(checks, scene_id)}
        # The checker that judged it: a later gate tells an older report's findings from a defect.
        if isinstance(checks, dict) and (judged := _int_or_none(checks.get("quality_report_version"))) is not None:
            drawing["report_version"] = judged
        if revision is not None:
            drawing["revision"] = int(revision)
        if stage:
            drawing["stage"] = str(stage)
        if fps:
            drawing["fps"] = float(fps)
        if (content or {}).get(scene_id):
            drawing["content"] = content[scene_id]
        # What its picture showed wrong: the gate refuses a render while the scene is drawn as it was.
        if picture := picture_findings(checks, scene_id):
            drawing["picture"] = picture
        # What nothing in the spec caused on it, failing or held: the next drawing of the same digest is held to it.
        if unexplained := unexplained_findings(checks, scene_id):
            drawing["unexplained"] = unexplained
        # What it read of the inserts on the scene's joins: another drawing of the same join is held to it.
        if joins := join_measures(checks, scene_id, seen, joined):
            drawing["joins"] = joins
        # What its inserts measured at their key and light: the next drawing is held to it.
        if inserts := insert_measures(checks, scene_id):
            drawing["inserts"] = inserts
        if (parts or {}).get(scene_id):
            drawing["parts"] = dict(parts[scene_id])
        earlier = [
            {k: v for k, v in d.items() if k != "parts"} for d in _drawings(kept.get(scene_id)) if d["digest"] != digest
        ]
        kept[scene_id] = {**drawing, "version": DIGEST_VERSION, "drawings": [drawing, *earlier][:DRAWINGS_KEPT]}
    return kept


def insert_measures(checks: dict | None, scene_id: str) -> dict[str, dict]:
    """What each screen insert of *scene_id* measured on a render's *checks*: ``{insert key:
    {key_settings, light, fit, rest, measures, inside}}`` from its report (``insert_checks.reports``) - its key and
    light, the digest of every other field it is drawn by, the value of each finding a setting is the advice for,
    and the residual key inside its quad -, kept on the scene's drawing (:func:`record_seen`) so the next drawing
    tells a finding no key or light setting moves. And how far off its bezel its quad was (``alignment``, the
    worst mean over its corners) and the size its frames were measured at (``measured_at``) - the measure a
    neighbour's preview at another size says (``insert_checks.bezel_readings``)."""
    from app.services.video_studio import timeline

    out: dict[str, dict] = {}
    for report in insert_reports(checks):
        name = str(report.get("key") or "")
        if not name or timeline.insert_scene(name) != scene_id:
            continue
        drawn = report.get("drawn_by") if isinstance(report.get("drawn_by"), dict) else {}
        measures = report.get("measures") if isinstance(report.get("measures"), dict) else {}
        if not measures and report.get("residual_inside") is None:
            continue
        record: dict[str, Any] = {
            "key_settings": drawn.get("key", report.get("key_settings")),
            "light": drawn.get("light"),
            "fit": drawn.get("fit", report.get("fit")),
            "rest": report.get("drawn_rest"),
            "measures": measures,
        }
        if isinstance(drawn.get("despill"), dict):
            record["despill"] = drawn["despill"]
        if report.get("residual_inside") is not None:
            record["inside"] = float(report["residual_inside"])
        if isinstance(report.get("verdicts"), dict) and report["verdicts"]:
            record["verdicts"] = {str(gate): bool(passed) for gate, passed in report["verdicts"].items()}
        if report.get("alignment_max") is not None and isinstance(report.get("measured_at"), list):
            record["alignment"] = float(report["alignment_max"])
            record["measured_at"] = list(report["measured_at"])
        out[name] = record
    return out


def insert_verdicts(previews: dict | None, digests: dict[str, str]) -> dict[str, list[dict]]:
    """``insert key -> [{stage, id, revision, gates}]``: whether each gate of each insert passed on the drawing of its
    scene as it is now (*digests*, :func:`drawing_of` - the same shot and spec), a preview or a whole render.
    What ``insert_checks.findings`` holds a render's own findings to: a gate that passed there and fails here
    disagrees with it, a warning. With how far off its bezel the drawing read its quad and at what size
    (``alignment``, ``measured_at``, :func:`insert_measures`), and the frame rate it was drawn at when recorded
    (``fps``): why a render at another rate disagrees with it."""
    out: dict[str, list[dict]] = {}
    for scene_id, digest in digests.items():
        drawing = drawing_of(previews, scene_id, digest)
        if drawing is None:
            continue
        for name, record in (drawing.get("inserts") or {}).items():
            gates = record.get("verdicts") if isinstance(record, dict) else None
            if not isinstance(gates, dict) or not gates:
                continue
            found = {
                "stage": drawing.get("stage") or "preview",
                "id": drawing.get("render_id"),
                "revision": drawing.get("revision"),
                "gates": dict(gates),
            }
            if record.get("alignment") is not None and isinstance(record.get("measured_at"), list):
                found.update(alignment=float(record["alignment"]), measured_at=list(record["measured_at"]))
            if drawing.get("fps"):
                found["fps"] = float(drawing["fps"])
            out.setdefault(str(name), []).append(found)
    return out


def earlier_measures(previews: dict | None) -> dict[str, list[dict]]:
    """``insert key -> [{key_settings, light, fit, rest, measures, inside, render_id}]``, newest first: what the drawings
    the video keeps (``VideoProject.scene_previews``, :func:`record_seen`) measured of each insert -
    what ``insert_checks.findings`` holds a finding a setting is the advice for to."""
    out: dict[str, list[dict]] = {}
    for entry in (previews or {}).values() if isinstance(previews, dict) else ():
        for drawing in _drawings(entry):
            for name, record in (drawing.get("inserts") or {}).items():
                if isinstance(record, dict):
                    out.setdefault(str(name), []).append({**record, "render_id": drawing.get("render_id")})
    return out


#: The action of a finding nothing in the spec causes (``insert_checks.NOTHING``): what the render drew, which
#: no edit of the video moves and rendering again draws the same - the render harness's defect, never the video's
#: (:func:`harness_defects`).
UNEXPLAINED_ACTION = "nothing"
#: Two such findings name the same frames when their spans in the plan (``data.span``) meet within this.
SAME_FRAMES_SECONDS = 0.05


#: The most frames a finding nothing in the spec causes may span and stay its harness's defect: one frame the
#: page drew and the encoder dropped. An insert lost on more is the video's - ``insert_missing``, or its measurement
#: lost (``insert_measurement_lost``) - and blocks, never said once and left.
HARNESS_MAX_FRAMES = 1


def is_unexplained(finding: Finding | dict) -> bool:
    """Whether *finding* is one nothing in the spec causes (:data:`UNEXPLAINED_ACTION`) on one frame alone
    (:data:`HARNESS_MAX_FRAMES`): the render harness's defect (:func:`harness_defects`)."""
    data = finding.data if isinstance(finding, Finding) else finding.get("data")
    chosen = (data or {}).get("action") if isinstance(data, dict) else None
    if not (isinstance(chosen, dict) and chosen.get("kind") == UNEXPLAINED_ACTION):
        return False
    frames = data.get("frames", data.get("absent", 1))
    return isinstance(frames, int | float) and int(frames) <= HARNESS_MAX_FRAMES


def unexplained_findings(checks: dict | None, scene_id: str) -> list[dict]:
    """The findings nothing in the spec causes a render's *checks* left on *scene_id* - its harness's defects
    (:func:`harness_of`), or a finding stored before them: kind, insert and span in the plan - what
    :func:`record_seen` keeps on the scene's drawing for the next one."""
    out = []
    for f in [*harness_of(checks), *findings_of(checks)]:
        data = f.get("data") if isinstance(f.get("data"), dict) else {}
        if f.get("scene_id") == scene_id and is_unexplained(f) and isinstance(data.get("span"), list):
            out.append({"kind": f.get("kind"), "insert": data.get("insert"), "span": list(data["span"])})
    return out[:8]


def unexplained_before(previews: dict | None, digests: dict[str, str]) -> dict[str, dict]:
    """``scene id -> {render_id, findings}``: the findings nothing in the spec causes that the drawing of each scene as it
    is now (*digests*, :func:`drawing_of` - the same digest) left (:func:`unexplained_findings`)."""
    out: dict[str, dict] = {}
    for scene_id, digest in digests.items():
        drawing = drawing_of(previews, scene_id, digest)
        if drawing is not None and drawing.get("unexplained"):
            out[scene_id] = {"render_id": drawing.get("render_id"), "findings": list(drawing["unexplained"])}
    return out


def join_measures(
    checks: dict | None, scene_id: str, seen: dict[str, str], joined: dict[str, str] | None
) -> dict[str, dict]:
    """``insert key -> {digest, present, missing}``: what a render's *checks* read of each insert on its joins
    (``presence.join_spans``), with the digest its scene was drawn at (*joined*), kept on *scene_id*'s drawing
    (:func:`record_seen`) - its own inserts, and those of a scene the render did not draw but read beside it (a scene
    preview's neighbour: the transition into or out of it)."""
    from app.services.video_studio import timeline

    out: dict[str, dict] = {}
    for report in insert_reports(checks):
        key = str(report.get("key") or "")
        spans = (report.get("presence") or {}).get("join_spans") if isinstance(report.get("presence"), dict) else None
        owner = timeline.insert_scene(key) if key else ""
        if not isinstance(spans, dict) or not (joined or {}).get(owner) or (owner != scene_id and owner in seen):
            continue
        out[key] = {
            "digest": joined[owner],
            "present": list(spans.get("present") or [])[:8],
            "missing": list(spans.get("missing") or [])[:8],
        }
    return out


def join_readings(previews: dict | None, digests: dict[str, str]) -> dict[str, list[dict]]:
    """``insert key -> [{stage, id, revision, present, missing}]``: what the drawings of the scenes as they are now
    (*digests*) read of each insert on its joins, its own scene drawn as it is now too (:func:`join_measures`) - two
    drawings of the same join, which ``insert_checks.agree_on_joins`` holds a new one to."""
    from app.services.video_studio import timeline

    out: dict[str, list[dict]] = {}
    for scene_id, digest in digests.items():
        drawing = drawing_of(previews, scene_id, digest)
        for key, record in ((drawing or {}).get("joins") or {}).items():
            if isinstance(record, dict) and record.get("digest") == digests.get(timeline.insert_scene(key)):
                out.setdefault(str(key), []).append(
                    {
                        "stage": drawing.get("stage") or "preview",
                        "id": drawing.get("render_id"),
                        "revision": drawing.get("revision"),
                        "present": list(record.get("present") or []),
                        "missing": list(record.get("missing") or []),
                    }
                )
    return out


def _same_frames(span: list, other: list) -> bool:
    return (
        float(span[0]) <= float(other[1]) + SAME_FRAMES_SECONDS
        and float(other[0]) <= float(span[1]) + SAME_FRAMES_SECONDS
    )


def harness_defects(findings: list[Finding], before: dict[str, dict]) -> tuple[list[Finding], list[Finding]]:
    """``(findings, defects)``: a render's *findings* parted into the video's and its harness's.

    A finding nothing in the spec causes (:func:`is_unexplained`: an insert
    missing or dropped on frames its spec shows it on, a layer not drawn
    where the page draws it, an insert asked past its painted end) is the
    render harness's defect by its own words: no edit of the video moves it,
    and rendering again draws the same frame. It leaves the findings - it
    never blocks nor warns -, kept on the render (``Checks.harness``) with the
    frame it shows, and said once to the administrator (``render_answer``:
    ``infra_once``, :func:`harness_line`): not again (``data.said`` false,
    ``repeated_from``) when the drawing of its scene with the same digest
    (*before*, :func:`unexplained_before`) left it on the same frames. The
    findings stay the same objects, so their frames are stored as any.
    """
    kept: list[Finding] = []
    defects: list[Finding] = []
    for finding in findings:
        if not is_unexplained(finding):
            kept.append(finding)
            continue
        span = finding.data.get("span")
        earlier = before.get(finding.scene_id or "") or {}
        again = isinstance(span, list) and any(
            record.get("kind") == finding.kind
            and record.get("insert") == finding.data.get("insert")
            and isinstance(record.get("span"), list)
            and _same_frames(span, record["span"])
            for record in earlier.get("findings") or []
        )
        finding.data["said"] = not again
        if again:
            finding.data["repeated_from"] = earlier.get("render_id")
        defects.append(finding)
    return kept, defects


def harness_line(defect: dict) -> str:
    """A harness defect (:func:`harness_of`) as a render's answer says it to the administrator: where, what,
    the frame that shows it - and that it is no finding of the video."""
    data = defect.get("data") or {}
    where = clock(defect.get("at")) + (f" scene `{defect['scene_id']}`" if defect.get("scene_id") else "")
    what = str(defect.get("message") or "")
    fix = str(defect.get("fix") or "")
    if fix and what.endswith(fix):
        what = what[: -len(fix)].rstrip().removesuffix("-").rstrip()
    frame = f" (frame: file `{data['frame_file_id']}`)" if data.get("frame_file_id") else ""
    return (
        f"render harness, {where} ({defect.get('kind')}): {what}{frame} - nothing in the video causes it and rendering "
        "again draws the same frame: report the render with this frame; no finding of the video, it blocks nothing"
    )


def picture_findings(checks: dict | None, scene_id: str) -> list[dict]:
    """The failing picture findings (:data:`PICTURE_KINDS`) a render's *checks* left on *scene_id*: kind, time, message."""
    return [
        {"kind": f.get("kind"), "at": f.get("at"), "message": str(f.get("message") or "")[:300]}
        for f in findings_of(checks)
        if f.get("scene_id") == scene_id
        and f.get("kind") in PICTURE_KINDS
        and f.get("severity") == FAIL
        and not is_unexplained(f)
    ][:4]


def reported_pictures(previews: dict | None, digests: dict[str, str]) -> dict[str, set[str]]:
    """``scene id -> the kinds of picture finding`` its drawing as it is now (*digests*) reported."""
    found: dict[str, set[str]] = {}
    for scene_id, digest in digests.items():
        drawing = drawing_of(previews, scene_id, digest) or {}
        kinds = {str(p.get("kind")) for p in drawing.get("picture") or [] if isinstance(p, dict) and p.get("kind")}
        if kinds:
            found[scene_id] = kinds
    return found


def settle_neighbours(checks: Checks, scene_id: str, reported: dict[str, set[str]]) -> list[Finding]:
    """A scene preview's verdict is its own scene's: a failing finding on a neighbour never fails it.

    A preview draws the scenes before and after its own through the
    transitions, so it may see a neighbour's screen insert (the crossfade
    out of the scene before shows its insert for a second). That finding
    stays on the scene that owns the insert: one its own drawing as it is now
    already reported (*reported*, :func:`reported_pictures`) is left out and
    said in a note; another is kept as a warning naming its scene, for that
    scene's own preview to judge. What blocks on the frames of a join
    (a key left there, an insert not drawn there - ``data.transition``)
    is this preview's own frames: kept a failure, naming that scene. A finding
    on *scene_id*, or on no scene, is untouched; each insert of a neighbour is
    marked so in ``checks.inserts``. Returns the findings left out.
    """
    import dataclasses

    kept: list[Finding] = []
    left_out: list[Finding] = []
    for finding in checks.findings:
        owner = finding.scene_id
        if finding.severity != FAIL or owner in (None, scene_id):
            kept.append(finding)
            continue
        if finding.data.get("transition"):
            finding.data["neighbour"] = True
            # A reworded message drops its code: the editor shows these words, not the bare finding's.
            kept.append(
                dataclasses.replace(
                    finding,
                    message=f"on scene {owner}, measured on this preview's join with it: {finding.message}",
                    code=None,
                    params=None,
                )
            )
            continue
        if finding.kind in reported.get(owner, set()):
            left_out.append(finding)
            checks.notes.append(
                f"Scene {owner}'s {finding.kind} at {clock(finding.at)} shows in this preview's transition: its own "
                "preview reports it, so it does not fail this one."
            )
            continue
        # The same data: the frame its finding shows is stored onto it later (``render._store_proofs``).
        finding.data["neighbour"] = True
        message = f"on scene {owner}, drawn beside this one (its own preview judges it): {finding.message}"
        kept.append(dataclasses.replace(finding, severity=WARN, message=message, code=None, params=None))
    checks.findings = kept
    for report in checks.inserts:
        if report.get("scene_id") not in (None, scene_id):
            report["neighbour"] = True
    return left_out


#: Two encodes of one drawn frame differ by encoder noise alone (dev:render-picture-check-gate-drift-2): their mean
#: absolute difference stays under this many grey levels ...
SAME_FRAME_MEAN_DIFF = 2.0
#: ... and no block of :data:`SAME_FRAME_BLOCK` pixels a side differs by more than this on average: a halo, a patch or a
#: mark drawn anew over a few hundred pixels moves its blocks past it.
SAME_FRAME_BLOCK_DIFF = 6.0
SAME_FRAME_BLOCK = 8


def frames_alike(one: Any, other: Any) -> bool:
    """Whether two frames (RGB arrays or pictures, the same size) are the same drawing under encoder noise
    (:data:`SAME_FRAME_MEAN_DIFF`, :data:`SAME_FRAME_BLOCK_DIFF`); frames of other sizes never are."""
    import numpy as np

    a = np.asarray(one.convert("RGB") if hasattr(one, "convert") else one, dtype=np.float32)
    b = np.asarray(other.convert("RGB") if hasattr(other, "convert") else other, dtype=np.float32)
    if a.shape != b.shape or a.ndim != 3:
        return False
    diff = np.abs(a - b).mean(axis=2)
    if float(diff.mean()) > SAME_FRAME_MEAN_DIFF:
        return False
    side = SAME_FRAME_BLOCK
    height, width = (diff.shape[0] // side) * side, (diff.shape[1] // side) * side
    if height == 0 or width == 0:
        return True
    blocks = diff[:height, :width].reshape(height // side, side, width // side, side).mean(axis=(1, 3))
    return float(blocks.max()) <= SAME_FRAME_BLOCK_DIFF


def passed_pictures(render: dict) -> bool:
    """Whether an earlier render's stored checks hold no failing picture finding (a forced render may be ready with
    some: it passed nothing)."""
    return not any(
        f.get("severity") == FAIL and f.get("kind") in PICTURE_KINDS for f in findings_of(render.get("checks"))
    )


def settle_new_gates(
    checks: Checks,
    earlier: list[dict],
    *,
    whole: dict[str, str],
    release: dict[str, int],
    alike: dict[int, dict] | None = None,
) -> list[Finding]:
    """A picture gate a later release added or tightened never silently fails frames a render of the same revision
    already passed (dev:render-picture-check-gate-drift).

    *earlier*: the ready whole renders of the same video, revision and quality, newest first, each ``{id, finished_at,
    checks, drawing}``. The first of them drawn alike - the same scene digests (*whole*, ``SceneDigests.whole``) and
    the same drawing release (*release*, :func:`release_drawing`), so the same frames - whose checks hold no failing
    picture finding is the one held to: each failing picture finding of *checks* is made a warning naming it, ``new
    gate since render <id>``, with ``data.new_gate_since``. A drawing that moved (an insert drawn again, a scene
    edited, another release's drawing) keeps every block.

    *alike* (dev:render-picture-check-gate-drift-2): ``index of a finding of checks.findings -> earlier render`` whose
    frame at the finding's moment, read off both MP4s, is the same drawing under encoder noise (:func:`frames_alike`)
    while that render passed its picture gates (:func:`passed_pictures`): such a failing picture finding is made the
    same advisory even when the digests or the drawing release moved (a release that redrew nothing there), with
    ``data.new_gate_since.matched = "frames"``. A finding whose frame differs keeps its block. Returns the findings made
    warnings.
    """
    import dataclasses

    passed = next(
        (
            render
            for render in earlier
            if isinstance(render.get("drawing"), dict)
            and render["drawing"].get("whole") == whole
            and render["drawing"].get("release") == release
            and passed_pictures(render)
        ),
        None,
    )
    alike = {index: render for index, render in (alike or {}).items() if passed_pictures(render)}
    if passed is None and not alike:
        return []

    def since_of(render: dict) -> str:
        when = render.get("finished_at")
        return f"render {str(render.get('id') or '')[:8]}" + (f" ({when})" if when else "")

    kept: list[Finding] = []
    settled: list[Finding] = []
    named: set[str] = set()
    for index, finding in enumerate(checks.findings):
        if finding.severity != FAIL or finding.kind not in PICTURE_KINDS:
            kept.append(finding)
            continue
        held_to, matched = (passed, "drawing") if passed is not None else (alike.get(index), "frames")
        if held_to is None:
            kept.append(finding)
            continue
        since = since_of(held_to)
        named.add(since)
        data = {
            **finding.data,
            "new_gate_since": {
                "render_id": str(held_to.get("id") or ""),
                "finished_at": held_to.get("finished_at"),
                "matched": matched,
            },
        }
        message = f"new gate since {since}, which passed the same frames (an advisory, not a block): {finding.message}"
        warned = dataclasses.replace(finding, severity=WARN, message=message, data=data, code=None, params=None)
        kept.append(warned)
        settled.append(warned)
    checks.findings = kept
    if settled:
        how = "drawn alike" if passed is not None else "its frames at those moments the same under encoder noise"
        checks.notes.append(
            f"{len(settled)} picture finding(s) are advisories: {', '.join(sorted(named))} of this revision, {how}, "
            "passed the same frames before a later release added or tightened their gate."
        )
    return settled


def _drawn(previews: dict | None) -> dict[str, list[dict]]:
    """``scene id -> its drawings`` hashed as :func:`scene_digests` hashes now; an entry of an older version is left out."""
    kept = previews if isinstance(previews, dict) else {}
    return {scene_id: found for scene_id, entry in kept.items() if (found := _drawings(entry))}


def drawing_of(previews: dict | None, scene_id: str, digest: str, content: str | None = None) -> dict | None:
    """The drawing of *scene_id* made of what *digest* hashes, if one is kept.

    Else, given the scene's *content* digest (:class:`SceneDigests`), the newest drawing of the same
    content that passed: an edit of the video's theme or music alone leaves what the scene shows as it was
    drawn, so its passing preview still counts - a drawing that failed never does (the theme may be its fix).
    """
    drawings = _drawn(previews).get(scene_id, [])
    exact = next((d for d in drawings if d["digest"] == digest), None)
    if exact is not None or not content:
        return exact
    return next((d for d in drawings if d.get("content") == content and d.get("passed") is True), None)


def changed_scenes(previews: dict | None, digests: dict[str, str], content: dict[str, str] | None = None) -> list[str]:
    """The scenes drawn before whose *digests* (:func:`scene_digests`) match none of their drawings: changed since.

    A scene never drawn is not changed: it never holds a render, and nothing of it is out of date. A scene
    whose edit was reverted is back to a drawing it has: not changed. Nor is a scene whose
    *content* digest matches a drawing that passed (:func:`drawing_of`).
    """
    drawn = _drawn(previews)
    return [
        scene_id
        for scene_id, digest in digests.items()
        if scene_id in drawn and drawing_of(previews, scene_id, digest, (content or {}).get(scene_id)) is None
    ]


def kept_scenes(previews: dict | None, digests: dict[str, str], content: dict[str, str]) -> list[str]:
    """The scenes no drawing shows as they are now that keep a passing drawing of the same content.

    What an edit of the video's theme or music alone leaves them: their preview still counts, said by the save.
    """
    drawn = _drawn(previews)
    return [
        scene_id
        for scene_id, digest in digests.items()
        if scene_id in drawn
        and all(d["digest"] != digest for d in drawn[scene_id])
        and drawing_of(previews, scene_id, digest, content.get(scene_id)) is not None
    ]


def restored_scenes(previews: dict | None, digests: dict[str, str]) -> list[str]:
    """The scenes whose *digests* are back to a drawing older than their last one: an edit reverted."""
    drawn = _drawn(previews)
    return [
        scene_id
        for scene_id, digest in digests.items()
        if scene_id in drawn
        and drawn[scene_id][0]["digest"] != digest
        and any(d["digest"] == digest for d in drawn[scene_id])
    ]


def passed_scenes(previews: dict | None, digests: dict[str, str], content: dict[str, str] | None = None) -> set[str]:
    """The scenes whose drawing as they are now - a preview or a whole render - passed its checks (or a passing
    drawing of their *content*)."""
    return {
        scene_id
        for scene_id, digest in digests.items()
        if (found := drawing_of(previews, scene_id, digest, (content or {}).get(scene_id))) is not None
        and found.get("passed") is True
    }


def holding_scenes(previews: dict | None, digests: dict[str, str], content: dict[str, str] | None = None) -> list[str]:
    """The scenes whose preview still holds, in the video's order: a passing drawing of each as it is now
    (:func:`passed_scenes`) - what the save and ``get_video`` list, so a render needs no preview of them."""
    passed = passed_scenes(previews, digests, content)
    return [scene_id for scene_id in digests if scene_id in passed]


def adopted(previews: dict | None, marks: SceneDigests) -> dict | None:
    """*previews* with each drawing an earlier release recorded of a scene as it is now carried to the digests it has
    now (:attr:`SceneDigests.aliases`): its ``digest``, its ``content`` and the digests its readings of the
    inserts on its joins were taken at. Read, never written: every reader of the drawings takes them through it, and
    a new drawing is recorded under the digests of now. *previews* as it is when no drawing needs carrying."""
    from app.services.video_studio import timeline

    names = marks.aliases if isinstance(marks.aliases, dict) else {}
    if not isinstance(previews, dict) or not names:
        return previews

    def carried(drawing: dict) -> dict:
        own = names.get(drawing.get("_scene", "")) or {}
        out = {k: v for k, v in drawing.items() if k != "_scene"}
        if drawing.get("digest") in own:
            out["digest"] = own[drawing["digest"]]
        if drawing.get("content") in own:
            out["content"] = own[drawing["content"]]
        if isinstance(drawing.get("joins"), dict):
            out["joins"] = {
                key: {
                    **record,
                    "digest": (names.get(timeline.insert_scene(key)) or {}).get(
                        record.get("digest"), record.get("digest")
                    ),
                }
                if isinstance(record, dict)
                else record
                for key, record in drawing["joins"].items()
            }
        return out

    out: dict = {}
    for scene_id, entry in previews.items():
        drawings = _drawings(entry)
        if not drawings:
            out[scene_id] = entry
            continue
        moved = [carried({**drawing, "_scene": scene_id}) for drawing in drawings]
        out[scene_id] = {
            **entry,
            **{k: moved[0][k] for k in ("digest", "content", "joins") if k in moved[0]},
            "drawings": moved,
        }
    return out


def changed_parts(previews: dict | None, scene_id: str, parts: dict[str, str] | None) -> list[str]:
    """What changed in *scene_id* since its newest drawing: the parts whose fingerprint (*parts*,
    :attr:`SceneDigests.parts`) differs from the one that drawing recorded - ``narration``, ``overlay 0 quad``,
    ``transition in``, ``scene before`` -, at most :data:`PARTS_NAMED` and how many more. Empty when that drawing
    recorded none (drawn by an earlier release) or nothing it recorded differs."""
    drawings = _drawn(previews).get(scene_id, [])
    then = drawings[0].get("parts") if drawings else None
    if not isinstance(then, dict) or not then or not parts:
        return []
    moved = [name for name in dict.fromkeys([*parts, *then]) if parts.get(name) != then.get(name)]
    # An overlay whose fields moved is said by those fields; ``overlay N`` alone when it was added, removed or its
    # set of fields changed and no field of it is named.
    named = [
        name
        for name in moved
        if not (name.startswith("overlay ") and name.count(" ") == 1 and any(m.startswith(f"{name} ") for m in moved))
    ]
    if len(named) > PARTS_NAMED:
        return [*named[:PARTS_NAMED], f"{len(named) - PARTS_NAMED} more"]
    return named


def changed_words(previews: dict | None, scene_id: str, parts: dict[str, str] | None) -> str:
    """``narration, transition in``: :func:`changed_parts` as a refusal and a save say it; empty when unknown."""
    return ", ".join(changed_parts(previews, scene_id, parts))


@dataclass(frozen=True)
class GateReason:
    """One reason the gate refuses a render: *message* is the English sentence the agent reads; *params*, when
    given, is what the editor builds the reason from in the reader's language (``RendersPanel.RenderGateDialog``),
    the message being its fallback."""

    code: str
    message: str
    scene_id: str | None = None
    params: dict | None = None

    def as_dict(self) -> dict:
        data = {"code": self.code, "message": self.message, "scene_id": self.scene_id}
        if self.params is not None:
            data["params"] = self.params
        return data


#: The reason a refused start names what its pre-render lint wrote before refusing: a quad snapped to its
#: bezel, saved as a new revision (``projects._lint`` commits it at once).
SNAPPED = "insert_snapped"


class RenderGateError(Exception):
    """``render_video`` refused by the quality gate: :attr:`reasons` say why; ``force`` renders anyway."""

    def __init__(self, reasons: list[GateReason]) -> None:
        self.reasons = reasons
        super().__init__(
            "The render was refused: "
            + "; ".join(reason.message for reason in reasons[:6])
            + ". Fix them, or render anyway (force)."
        )


#: The quality findings a render waits for: what the audience would see; an advisory one never blocks.
BLOCKING_SEVERITIES = ("overloaded", "dense")
#: What a render does with a shot that is not ready, by where the scene shows it, and how that place is said.
_SHOT_OUTCOMES = {
    "footage": "a render refuses it",
    "backdrop": "a render refuses it",
    "insert": "a render refuses it",
    "animation": "a render plays the still image instead",
}
_PLACES = {
    "footage": "its",
    "backdrop": "the backdrop's",
    "insert": "its screen insert's",
    "animation": "its animation's",
}


def _shown(scene: Any) -> list[tuple[str, str, str]]:
    """``(where, kind, id)`` of what *scene* (parsed) plays: its recording, its shots, by where it shows them."""
    shown: list[tuple[str, str, str]] = []
    if scene.type == "recording" and scene.take_id:
        shown.append(("footage", "take", scene.take_id))
    if scene.type == "clip" and scene.clip_id:
        shown.append(("footage", "clip", scene.clip_id))
    if scene.type == "statement" and scene.backdrop is not None and scene.backdrop.clip_id:
        shown.append(("backdrop", "clip", scene.backdrop.clip_id))
    if scene.type == "image" and scene.animate is not None and scene.animate.clip_id:
        shown.append(("animation", "clip", scene.animate.clip_id))
    for overlay in scene.overlays:
        for source in overlay.shown_sources:
            if source.clip_id:
                shown.append(("insert", "clip", source.clip_id))
            elif source.take_id:
                shown.append(("insert", "take", source.take_id))
    return shown


def _not_ready(kind: str, row: Any) -> str | None:
    """Why *row* (a shot or a recording) cannot play yet; ``None`` when it can."""
    from app.models.video_studio import JOB_PENDING, JOB_READY, JOB_RUNNING
    from app.services.video_studio import clips

    if row is None:
        return "was deleted"
    if getattr(row, "expired_at", None) is not None:
        return "expired with the media retention"
    if kind == "clip" and clips.playable(row):
        return None
    if kind == "take" and row.status == JOB_READY and row.file_id:
        return None
    if row.status in (JOB_PENDING, JOB_RUNNING):
        return "is still being made"
    return f"{row.status} ({(getattr(row, 'error', None) or 'no reason given')[:160].rstrip('. ')})"


async def _media_reasons(db: Any, spec: Any) -> list[GateReason]:
    """The shots and recordings the scenes play that are not ready, each with what a render does with it."""
    from app.core.utils import safe_uuid
    from app.models.video_studio import VideoClip, VideoTake

    reasons: list[GateReason] = []
    for scene in spec.scenes:
        for where, kind, ref in _shown(scene):
            uid = safe_uuid(str(ref))
            row = await db.get(VideoClip if kind == "clip" else VideoTake, uid) if uid is not None else None
            why = _not_ready(kind, row)
            if why is None:
                continue
            what = "AI shot" if kind == "clip" else "recording"
            name = f' "{row.name[:60]}"' if row is not None and getattr(row, "name", None) else ""
            reasons.append(
                GateReason(
                    "media_not_ready",
                    f"scene {scene.id}: {_PLACES[where]} {what} `{ref}`{name} {why} - {_SHOT_OUTCOMES[where]}; wait "
                    "for it (get_video_jobs) or generate it again",
                    scene.id,
                )
            )
    return reasons


def shot_scenes(spec: Any) -> dict[str, set[str]]:
    """``clip id -> the scenes showing it`` of *spec* (parsed): as their footage, backdrop, screen insert or animation."""
    found: dict[str, set[str]] = {}
    for scene in spec.scenes:
        for _where, kind, ref in _shown(scene):
            if kind == "clip":
                found.setdefault(str(ref), set()).add(scene.id)
    return found


def cleared_shots(spec: Any, passed: set[str]) -> set[str]:
    """The AI shots every scene showing them passed a preview of, drawn as it is now: their findings are cleared.

    The preview drew the take that plays at the scene's trim, and its checks
    passed: what the quality check reads of the shot alone (its camera, its
    text) is no reason left to refuse the render.
    """
    return {clip_id for clip_id, scenes in shot_scenes(spec).items() if scenes and scenes <= passed}


def cleared_by(
    spec: Any, previews: dict | None, digests: dict[str, str], content: dict[str, str] | None = None
) -> dict[str, dict]:
    """``clip id -> {render_id, revision, scenes}`` of each AI shot :func:`cleared_shots` clears: the newest
    passing drawing, as its scenes are now, that cleared it - what the quality check says a cleared finding was
    cleared by."""
    passed = passed_scenes(previews, digests, content)
    out: dict[str, dict] = {}
    for clip_id, scenes in shot_scenes(spec).items():
        if not scenes or not scenes <= passed:
            continue
        drawings = [
            drawing
            for scene_id in sorted(scenes)
            if (drawing := drawing_of(previews, scene_id, digests[scene_id], (content or {}).get(scene_id))) is not None
        ]
        newest = max(drawings, key=lambda drawing: str(drawing.get("at") or ""))
        out[clip_id] = {
            "render_id": newest.get("render_id"),
            "revision": newest.get("revision"),
            "scenes": sorted(scenes),
        }
    return out


async def _quality_reasons(project: Any, user: Any, cleared: set[str], *, preview: bool = False) -> list[GateReason]:
    """The findings the audience would see in the quality check of the revision: its pictures and its
    sound read fresh, its layout as saved for the revision - measured when none is kept (text fit and overlap
    are never skipped), the page playing the plan without its media, no frame drawn.

    A shot not ready is said once, by :func:`_media_reasons`; a finding about a
    shot in *cleared* (:func:`cleared_shots`) is none, nor, for a *preview*, any
    finding about a shot: a preview that passes is what clears it.
    """
    from app.services.video_studio import quality

    report = await quality.check_video_quality(
        project.id, user.id, expected_revision=project.revision, measure=False, measure_layout=True
    )
    return [
        GateReason("quality", f"scene {f.scene_id}: {f.message} ({f.kind}, {f.severity})", f.scene_id)
        for f in report.findings
        if f.severity in BLOCKING_SEVERITIES
        and f.kind != "shot_state"
        and not (f.clip_id and (preview or f.clip_id in cleared))
    ]


async def target_quality(db: Any, project: Any, *, default: str | None = None) -> str:
    """The quality a save judges the scale its legibility advice offers at: *project*'s newest whole render's,
    the quality its author renders it at; *default* (``spec.DEFAULT_QUALITY`` unless given) before its first, or when
    it cannot be read. A save holds its recordings to the quality a final is rendered at
    (``spec.FINAL_QUALITY``)."""
    from sqlalchemy import select

    from app.core.utils import format_exception_message
    from app.models.video_studio import VideoRender
    from app.services.video_studio.spec import DEFAULT_QUALITY, QUALITY_PRESETS

    fallback = default if default in QUALITY_PRESETS else DEFAULT_QUALITY
    if project is None or getattr(project, "id", None) is None:
        return fallback
    try:
        newest = (
            await db.execute(
                select(VideoRender.quality)
                .where(VideoRender.project_id == project.id, VideoRender.scene_id.is_(None))
                .order_by(VideoRender.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
    except Exception as exc:  # advice judged at the default quality
        logger.info("Video %s render quality not read: %s", getattr(project, "id", "?"), format_exception_message(exc))
        return fallback
    return newest if newest in QUALITY_PRESETS else fallback


@dataclass(frozen=True)
class InsertGeometry:
    """What the screen inserts of a video are drawn from, read off the rows: their sources' sizes by
    ``(kind, id)``, each scene's footage size, a recording's pixel ratio, a shot's replaced screen, and how far down
    a recording's page keeps its layout (``VideoTake.layout``).

    A recording's pixel ratio is its script's - device pixels per pixel of its window -, and its browser zoom
    (percent) beside it: a zoomed recording lays its page out at their product (``spec.zoomed_window``).
    """

    sources: dict[tuple[str, str], tuple[int, int]]
    footage: dict[str, tuple[int, int]]
    pixel_ratios: dict[str, float]
    screens_replaced: dict[str, dict]
    browser_zooms: dict[str, int] = field(default_factory=dict)
    layouts: dict[str, dict] = field(default_factory=dict)


async def insert_geometry(db: Any, spec: Any) -> InsertGeometry:
    """The :class:`InsertGeometry` of the parsed *spec*: a recording's window and pixel ratio, a shot's size, a
    picture's stored geometry, a replaced screen's record; a source of unknown size is left out."""
    from app.core.utils import safe_uuid
    from app.models.video_studio import VideoClip, VideoTake
    from app.services.video_studio import preview, timeline
    from app.services.video_studio.clips import SCREEN, take_params
    from app.services.video_studio.spec import browser_zoom, painted_size, recorded_ratio

    sources: dict[tuple[str, str], tuple[int, int]] = {}
    pixel_ratios: dict[str, float] = {}
    browser_zooms: dict[str, int] = {}
    layouts: dict[str, dict] = {}
    rows: dict[tuple[str, str], Any] = {}

    async def row_of(kind: str, ref: str) -> Any:
        if (kind, ref) not in rows:
            uid = safe_uuid(str(ref))
            rows[(kind, ref)] = (
                await db.get(VideoTake if kind == "take" else VideoClip, uid) if uid is not None else None
            )
        return rows[(kind, ref)]

    async def size_of(kind: str, ref: str) -> tuple[int, int] | None:
        if kind == "take":
            take = await row_of(kind, ref)
            if take is None or not take.viewport_width or not take.viewport_height:
                return None
            ratio = float(take.pixel_ratio or 1.0)
            zoom = browser_zoom(take.script)
            pixel_ratios[ref] = recorded_ratio(ratio, take.script)
            browser_zooms[ref] = zoom
            if isinstance(getattr(take, "layout", None), dict):
                layouts[ref] = take.layout
            return painted_size((take.viewport_width, take.viewport_height), ratio)
        if kind == "clip":
            clip = await row_of(kind, ref)
            return (int(clip.width), int(clip.height)) if clip is not None and clip.width and clip.height else None
        if kind == "file":
            try:
                return await preview.overlay_picture_size(db, ref)
            except Exception:  # a file that is not a picture, or gone: the render says so
                return None
        return None

    for part in timeline.insert_sources(spec).values():
        if (part.kind, part.ref) not in sources:
            found = await size_of(part.kind, part.ref)
            if found is not None:
                sources[(part.kind, part.ref)] = found
    footage: dict[str, tuple[int, int]] = {}
    screens_replaced: dict[str, dict] = {}
    for scene in spec.scenes:
        if scene.type == "recording" and scene.take_id:
            take = await row_of("take", scene.take_id)
            if take is not None and take.viewport_width and take.viewport_height:
                footage[scene.id or ""] = (int(take.viewport_width), int(take.viewport_height))
        elif scene.type == "clip" and scene.clip_id:
            clip = await row_of("clip", scene.clip_id)
            if clip is not None and clip.width and clip.height:
                footage[scene.id or ""] = (int(clip.width), int(clip.height))
            screen = take_params(clip.params).get(SCREEN) if clip is not None else None
            if isinstance(screen, dict) and screen.get("picture_size"):
                screens_replaced[scene.clip_id] = screen
    return InsertGeometry(sources, footage, pixel_ratios, screens_replaced, browser_zooms, layouts)


async def _soft_reasons(db: Any, spec: Any, quality: str) -> list[GateReason]:
    """The screen inserts and replaced screens too soft for a render at *quality* (``insert_checks.soft_before``).

    Read off the rows (:func:`insert_geometry`), so a 4K render is refused
    before it is paid for, naming the source and the size (a recording: the
    pixel ratio) it needs.
    """
    from app.services.video_studio import insert_checks
    from app.services.video_studio.spec import QUALITY_PRESETS, output_size

    size = output_size(spec.format.aspect_ratio, QUALITY_PRESETS[quality].short_side)
    found = await insert_geometry(db, spec)
    return [
        GateReason("insert_soft", message, scene_id)
        for scene_id, message in insert_checks.soft_before(
            spec,
            size,
            sources=found.sources,
            footage=found.footage,
            pixel_ratios=found.pixel_ratios,
            screens_replaced=found.screens_replaced,
        )
    ]


async def soft_qualities(db: Any, spec: Any) -> list[Any]:
    """The screen inserts and replaced screens of *spec* (parsed, or a spec as stored) a whole render would refuse as
    soft at one of the qualities a video may be rendered at (``insert_checks.soft_by_quality``), read off the
    rows as the gate reads them (:func:`insert_geometry`): what a save and a scene preview advise. Never raises: advice
    that cannot be read is not given."""
    from app.core.utils import format_exception_message
    from app.services.video_studio import insert_checks
    from app.services.video_studio.spec import parse_video_spec

    try:
        parsed = parse_video_spec(spec) if isinstance(spec, dict) else spec
        found = await insert_geometry(db, parsed)
        return insert_checks.soft_by_quality(
            parsed,
            sources=found.sources,
            footage=found.footage,
            pixel_ratios=found.pixel_ratios,
            screens_replaced=found.screens_replaced,
        )
    except Exception as exc:
        logger.info("Screen insert resolution not read for every render quality: %s", format_exception_message(exc))
        return []


async def soft_recordings(db: Any, spec: Any, quality: str) -> list[Any]:
    """The recordings the screen inserts of the parsed *spec* draw too soft for a whole render at *quality*
    (``insert_checks.soft_takes``), read off the rows as the gate reads them (:func:`insert_geometry`), each with the
    recording that passes there: what a save refuses. Never raises: a reading that cannot be made refuses nothing, and
    the render's gate reads it again at the quality asked."""
    from app.core.utils import format_exception_message
    from app.services.video_studio import insert_checks
    from app.services.video_studio.spec import QUALITY_PRESETS, output_size

    try:
        size = output_size(spec.format.aspect_ratio, QUALITY_PRESETS[quality].short_side)
        found = await insert_geometry(db, spec)
        return insert_checks.soft_takes(
            spec,
            size,
            quality,
            sources=found.sources,
            footage=found.footage,
            pixel_ratios=found.pixel_ratios,
            zooms=found.browser_zooms,
        )
    except Exception as exc:
        logger.info("Screen insert recordings not judged at %s: %s", quality, format_exception_message(exc))
        return []


async def soft_ahead(db: Any, effective: dict, *, scene_id: str, quality: str) -> list[str]:
    """What a preview of *scene_id* drawn at *quality* advises: its screen inserts and replaced screen it draws
    sharp that a whole render at a larger quality would refuse as soft (:func:`soft_qualities`). One soft at the
    preview's own size is its lint's reason (:func:`static_reasons`), never said twice. Never refuses anything."""
    from app.services.video_studio.spec import QUALITY_PRESETS

    drawn = QUALITY_PRESETS[quality].short_side
    return [
        item.message()
        for item in await soft_qualities(db, effective)
        if item.scene_id == scene_id and QUALITY_PRESETS[item.qualities[0]].short_side > drawn
    ]


@dataclass(frozen=True)
class SizedRecording:
    """A recording sized for a screen insert of a saved video at one render quality (:func:`size_recording`)."""

    scene_id: str
    index: int | None
    quality: str
    #: The render's frame, and the surfaces the recording is drawn on there.
    size: tuple[int, int]
    surfaces: list[Any]
    #: ``insert_checks.Recording``; ``None`` when no recording a browser can make passes the check there.
    recording: Any
    #: The frame rate the video renders at that quality: the rate the recording is paced at.
    fps: int = 30

    def words(self) -> str:
        """``Sized for an ultra render (3840x2160) of scene `s1`'s screen insert 0, its quad 3575x2389 px there: ...``."""
        from app.services.video_studio import screen_insert, timeline

        width, height = self.size
        largest = max(self.surfaces, key=lambda surface: surface.drawn[0] * surface.drawn[1])
        quad_w, quad_h = largest.quad
        where = timeline.insert_label(largest.key)
        if len(self.surfaces) > 1:
            where += f" and {len(self.surfaces) - 1} other surface(s) of the scene, the largest"
        pushed = f", pushed in {largest.push:g}x by its zoom" if largest.push > 1 else ""
        head = (
            f"Sized for a{'n' if self.quality == 'ultra' else ''} {self.quality} render ({width}x{height}) of scene "
            f"`{self.scene_id}`: {where}, its quad {round(quad_w)}x{round(quad_h)} px there "
            f"({quad_w / max(quad_h, 1e-6):.2f}:1, the window's proportions){pushed}"
        )
        recording = self.recording
        if recording is None:
            return head + "."
        view_w, view_h = recording.viewport
        zoom = (
            f" with a browser zoom of {recording.browser_zoom}% (its page laid out {recording.css_width} px wide)"
            if recording.browser_zoom != 100
            else ""
        )
        return (
            f"{head}: a {view_w}x{view_h} window at pixel ratio {recording.pixel_ratio:g}{zoom}, its frames "
            f"{recording.frame[0]}x{recording.frame[1]} px - {recording.density.value:.2f} source pixels per pixel "
            f"where it is drawn smallest (at least {screen_insert.MIN_DENSITY:g})."
        )


async def size_recording(
    db: Any,
    project: Any,
    user: Any,
    *,
    quality: str,
    scene: object,
    index: int | None,
    viewport: tuple[int, int],
    pixel_ratio: float | None,
    zoom: int,
) -> SizedRecording:
    """The recording a screen insert of *project*'s *scene* needs to pass the soft check at a whole render of *quality*
    (``insert_checks.recording_for``): its quads read off the saved spec as *user* sees it, its footage off the
    rows as the gate reads them (:func:`insert_geometry`), the recording laid out as a window of *viewport*'s width at
    the largest quad's proportions (``insert_checks.quad_window``) at *zoom* percent - from *pixel_ratio* up when
    the author asked one. Raises ``VideoSpecError`` for a scene or an insert the video does not have, or one whose quad
    is still left to the render."""
    from app.services.video_studio import insert_checks, projects
    from app.services.video_studio.spec import (
        QUALITY_PRESETS,
        SpecProblem,
        VideoSpecError,
        output_size,
        parse_video_spec,
    )

    applied = await projects.effective_spec(db, project, user)
    scene_id = projects.resolve_scene(dict(project.spec or {}), applied.spec, scene)
    spec = parse_video_spec(applied.spec)
    shown = next((s for s in spec.scenes if s.id == scene_id), None)
    inserts = [n for n, overlay in enumerate(shown.overlays if shown else []) if overlay.type == "screen_insert"]
    if not inserts:
        raise VideoSpecError([SpecProblem("scene", f"scene `{scene_id}` has no screen insert to size a recording for")])
    if index is None and len(inserts) > 1:
        raise VideoSpecError(
            [
                SpecProblem(
                    "insert",
                    f"scene `{scene_id}` has {len(inserts)} screen inserts ({', '.join(map(str, inserts))}): name one with insert",
                )
            ]
        )
    if index is not None and index not in inserts:
        raise VideoSpecError(
            [
                SpecProblem(
                    "insert",
                    f"scene `{scene_id}` has no screen insert {index}: its screen inserts are {', '.join(map(str, inserts))}",
                )
            ]
        )
    size = output_size(spec.format.aspect_ratio, QUALITY_PRESETS[quality].short_side)
    geometry = await insert_geometry(db, spec)
    surfaces = insert_checks.surfaces_at(spec, size, geometry.footage, scene_id=scene_id, index=index)
    if not surfaces:
        raise VideoSpecError(
            [
                SpecProblem(
                    "insert",
                    f"scene `{scene_id}`'s screen insert has no quad yet (quad auto is found on its footage when the "
                    "scene is previewed): give its quad, or preview the scene first",
                )
            ]
        )
    # Recorded at the drawn screen's proportions: a 1.9:1 screen is never a 16:9 window letterboxed in it.
    window = insert_checks.quad_window(surfaces, viewport)
    recording = insert_checks.recording_for(surfaces, window, least_ratio=pixel_ratio or 1.0, zoom=zoom)
    fps = int(spec.format.fps or QUALITY_PRESETS[quality].fps)
    return SizedRecording(scene_id, index if index is not None else inserts[0], quality, size, surfaces, recording, fps)


async def static_reasons(
    db: Any,
    project: Any,
    user: Any,
    effective: dict,
    *,
    quality: str,
    scene_id: str | None = None,
    cleared: set[str] | None = None,
) -> list[GateReason]:
    """The pre-render lint: every fault of *project* a render at *quality* would find, read without drawing a
    frame - its media not ready or gone, the quality check's findings the audience would see (a quad off its bezel, a
    surface not flat, footage shorter than its narration, a frozen hold, an insert past its loop seam, text that does
    not fit or covers other text) and a source too soft for its quad. *scene_id*: those of that scene alone,
    what its preview would draw; *cleared* the shots whose every scene passed a preview (:func:`cleared_shots`)."""
    from app.services.video_studio.spec import parse_video_spec

    spec = parse_video_spec(effective)
    reasons: list[GateReason] = [
        *await _media_reasons(db, spec),
        *await _quality_reasons(project, user, cleared or set(), preview=scene_id is not None),
        # A source too small for its quad at this quality: said before a 4K render is paid for.
        *await _soft_reasons(db, spec, quality),
    ]
    if scene_id is not None:
        reasons = [reason for reason in reasons if reason.scene_id == scene_id]
    return reasons


async def gate(db: Any, project: Any, user: Any, effective: dict, *, quality: str) -> list[GateReason]:
    """Why a whole render of *project* at *quality* should not start now (empty: it may): the pre-render lint
    (:func:`static_reasons`), then what its previews and earlier renders left."""
    from sqlalchemy import select

    from app.models.video_studio import JOB_FAILED, JOB_READY, VideoRender
    from app.services.video_studio import lexicon_store, voice, voice_check
    from app.services.video_studio.spec import parse_video_spec

    spec = parse_video_spec(effective)
    lexicon = await lexicon_store.for_voiceover(db, spec.voiceover)
    marks = await digest_scenes(db, effective, lexicon)
    # Read as the digests are hashed now: a drawing an earlier release recorded of a scene as it is now counts.
    seen = adopted(project.scene_previews if isinstance(project.scene_previews, dict) else {}, marks) or {}
    digests, content = marks.whole, marks.content
    passed = passed_scenes(seen, digests, content)
    reasons: list[GateReason] = await static_reasons(
        db, project, user, effective, quality=quality, cleared=cleared_shots(spec, passed)
    )
    if spec.voiceover.enabled and any(scene.narration for scene in spec.scenes):
        try:
            target = await voice.resolve(db, user.id, spec.voiceover)
        except voice.VoiceUnavailableError:
            target = None
        except Exception as exc:  # a speech model that cannot be read is the render's to report
            logger.debug("Voice not resolved for the render gate: %s", exc)
            target = None
        if target is not None:
            for scene_id, record in await voice_check.failed_narrations(target, spec, lexicon):
                reasons.append(
                    GateReason(
                        "narration_failed",
                        f"scene {scene_id}'s narration failed its transcript check ({record.get('reason') or 'failed'}): "
                        "reword it or change its voice, then preview the scene (render_video_scene)",
                        scene_id,
                    )
                )
    recent = list(
        (
            await db.execute(
                select(VideoRender)
                .where(
                    VideoRender.project_id == project.id,
                    VideoRender.revision == project.revision,
                    VideoRender.scene_id.is_(None),
                    VideoRender.status.in_((JOB_READY, JOB_FAILED)),
                    VideoRender.checks.is_not(None),
                )
                .order_by(VideoRender.created_at.desc())
                .limit(20)
            )
        )
        .scalars()
        .all()
    )
    # The latest render of the revision for what failed; the latest at this quality for a frozen picture.
    latest = recent[0] if recent else None
    alike = next((r for r in recent if r.quality == quality), None)

    def superseded(render: Any, finding: dict) -> bool:
        # A newer drawing of its scene - a preview made since - drew it as it is now and passed.
        scene_id = finding.get("scene_id")
        if scene_id not in passed:
            return False
        return (drawing_of(seen, scene_id, digests[scene_id], content.get(scene_id)) or {}).get("render_id") != str(
            render.id
        )

    for render, left in (
        (latest, open_findings(latest.checks, same_quality=False) if latest is not None else []),
        (
            alike,
            [f for f in open_findings(alike.checks, same_quality=True) if f.get("kind") in HELD_WARNINGS]
            if alike
            else [],
        ),
    ):
        found = [f for f in left if render is not None and not superseded(render, f)]
        if render is None or not found:
            continue
        listed = ", ".join(f"{f.get('kind')} at {clock(f.get('at'))}" for f in found[:4])
        created = getattr(render, "created_at", None)
        reasons.append(
            GateReason(
                "open_findings",
                f"render `{render.id}` ({render.quality}) of this same revision left {len(found)} finding(s) open "
                f"({listed}): change what they point at first, or preview their scenes again "
                "(render_video_scene): the same revision renders the same",
                # What a person reads: the render by its quality and time, never its id; each kind as a key the
                # editor labels (FINDING_KIND_LABELS).
                params={
                    "count": len(found),
                    "quality": render.quality,
                    "created_at": created.isoformat() if created is not None else None,
                    "findings": [
                        {"kind": str(f.get("kind")), "at": f.get("at") if isinstance(f.get("at"), int | float) else None}
                        for f in found[:4]
                    ],
                },
            )
        )
    for scene_id in changed_scenes(seen, digests, content):
        # What changed in it: the parts whose fingerprint moved since its newest drawing, when it kept them.
        what = changed_words(seen, scene_id, marks.parts.get(scene_id))
        reasons.append(
            GateReason(
                "scene_changed",
                f"scene {scene_id} changed since it was last previewed or rendered"
                + (f" ({what})" if what else "")
                + f": preview it again (render_video_scene(scene='{scene_id}'))",
                scene_id,
            )
        )
    # A scene drawn as it is now whose picture failed: the same scene draws the same.
    failed = {
        scene_id: drawing
        for scene_id, digest in digests.items()
        if (drawing := drawing_of(seen, scene_id, digest) or {}).get("picture")
    }
    judged = await _drawing_report_versions(db, failed.values())
    for scene_id, drawing in failed.items():
        listed = "; ".join(f"{p.get('kind')} at {clock(p.get('at'))}" for p in drawing["picture"][:3])
        version = judged.get(str(drawing.get("render_id")))
        reasons.append(
            GateReason(
                "scene_findings",
                f"scene {scene_id}'s last drawing failed its picture check ({listed}) "
                + failed_drawing_words(version, scene_id),
                scene_id,
            )
        )
    return reasons


async def _drawing_report_versions(db: Any, drawings: Any) -> dict[str, int | None]:
    """The quality report version each of *drawings* (``VideoProject.scene_previews`` drawings) was judged under, by
    render id: the one it recorded, else - a drawing kept before it recorded one - its render's stored checks'."""
    import uuid

    from sqlalchemy import select

    from app.models.video_studio import VideoRender

    # A render id no row answers for (purged, replaced) stays None: its version is unknown, not current.
    versions: dict[str, int | None] = {}
    unknown: list[uuid.UUID] = []
    for drawing in drawings:
        render_id = str(drawing.get("render_id") or "")
        if not render_id:
            continue
        if drawing.get("report_version") is not None:
            versions[render_id] = _int_or_none(drawing.get("report_version"))
            continue
        try:
            unknown.append(uuid.UUID(render_id))
        except ValueError:
            versions[render_id] = None
    if unknown:
        rows = await db.execute(select(VideoRender.id, VideoRender.checks).where(VideoRender.id.in_(unknown)))
        for render_id, checks in rows.all():
            versions[str(render_id)] = (
                _int_or_none((checks or {}).get("quality_report_version")) if isinstance(checks, dict) else None
            )
    return versions
