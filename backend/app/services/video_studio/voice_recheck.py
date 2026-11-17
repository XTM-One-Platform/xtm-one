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

"""A narration a ready render plays with a fallback take, re-checked once the voice pipeline changed.

A narration the video's voice fails at its speed is placed with a fallback
take (``voice_check.fallback_kind``): slower (``voice.SLOWER``, 0.85 of its
speed), a term set apart in its clause (``lexicon.REFRAMED``), another voice.
A later change of the voice pipeline - the check's rules, how the lexicon
sends a term, the retry ladder (``voice_check.pipeline_version``) - or of the
lexicon entry of the term it failed on may let it pass at its speed, yet the
ready render kept being answered "already covers this revision", the slower
take in the final for good. So:

* **What a render records.** Each whole render keeps, beside what it draws
  and plays (``VideoRender.drawing``), the take each narration plays
  (``takes``: its object in the voice cache, its record's digest) and each
  scene playing a fallback take (``fallbacks``: the take, why, its fix, and
  its reuse digest - ``render_checks.fallback_digest``: what its narration is
  said with and the voice pipeline), and what its frames were drawn from
  (``encoded_as``, ``compositor.drawing_digest``). :func:`played`.
* **The offer** (:func:`offered`). A start reading that render
  (``projects.covering_render``) offers a re-check of each scene whose
  fallback digest moved - the pipeline changed, or the lexicon entries its
  narration writes - when nothing else moved: the frames' inputs but for the
  narrations' lengths (``SceneDigests.picture``), the sound beside the voice
  (``beside``) and every other narration's lexicon (``said``). A render
  playing no fallback, or whose fallbacks did not move, is answered as before.
  A scene re-checked to no avail is settled (:func:`settle`): never offered
  again for the same digest.
* **The re-check** (:func:`recheck`). ``render_video`` asks the user (in an
  automation, it goes on): each scene offered is measured again - spoken
  anew at the video's speed past the voice cache and heard back with the
  transcript check ``preview_voiceover`` runs (``voice_check.speak_checked``
  with ``fresh``, ``voice_check.hear_back``), never a record's figure
  replayed. ``render_video(recheck: true)`` measures every narration the
  voice cache places with a fallback take (:func:`fallback_scenes`) before
  it starts, offered or not, and names each result (:meth:`Rechecked.said`).
  One that now passes at the video's speed in its own voice is spliced: a
  new whole render (:class:`Splice`, ``projects.start_render``) places
  every other narration with the take the ready render played
  (``voice_check.place_kept``), and, when its frames are drawn from
  exactly what the ready render's were (``encoded_as``), muxes its new
  sound onto that render's MP4 - no frame drawn, its picture checks that
  render's. A take that changes what is drawn (a scene's length or its
  burned-in captions follow its narration) is drawn again, said so.
"""

from __future__ import annotations

import logging
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.core.utils import format_exception_message

logger = logging.getLogger(__name__)

#: Where a whole render that splices a re-checked take keeps what it splices (``VideoRender.drawing``).
SPLICE = "splice"
#: Why a scene is offered a re-check: the voice pipeline moved, the lexicon entries its narration writes did.
PIPELINE_MOVED = "the voice check changed since"
LEXICON_MOVED = "its lexicon entry changed since"


@dataclass(frozen=True)
class Recheck:
    """A scene of a ready render offered a re-check (:func:`offered`): its fallback take and why."""

    scene_id: str
    take: Any
    why: str
    #: Its fallback digest now (``render_checks.fallback_digest``): what :func:`settle` keeps once re-checked.
    digest: str

    def said(self) -> str:
        """``scene `s2` plays a 0.85 take, the voice check changed since``."""
        return f"scene `{self.scene_id}` plays {self.take.take()}, {self.why}"


@dataclass
class Offer:
    """The ready render that covers a revision's picture, and the scenes of it a re-check is offered for."""

    render: Any
    rechecks: list[Recheck] = field(default_factory=list)


@dataclass(frozen=True)
class Splice:
    """A whole render splicing re-checked takes into a ready render (``VideoRender.drawing["splice"]``)."""

    #: The ready render whose frames and other takes it keeps.
    source: str
    #: The scenes re-checked that now pass at the video's speed: spoken as the voice check places them now.
    scenes: tuple[str, ...]

    def as_dict(self) -> dict:
        return {"source": self.source, "scenes": list(self.scenes)}

    @classmethod
    def of(cls, raw: object) -> Splice | None:
        if not isinstance(raw, dict) or not raw.get("source"):
            return None
        return cls(str(raw["source"]), tuple(str(sid) for sid in raw.get("scenes") or [] if sid))


@dataclass(frozen=True)
class Source:
    """What a splice reads of the ready render it splices into, once (``render._run_render``)."""

    render_id: str
    file_id: str | None
    encoded_as: str | None
    takes: dict[str, dict]
    #: Its picture checks (``checks.frames``, ``checks.inserts``): the verdict on the very frames it keeps.
    frames: dict | None
    inserts: list[dict]
    #: Its cut joins as the cut gate read them (``checks.cuts``, issue #8935), kept with the frames.
    cuts: dict | None = None

    @classmethod
    def of(cls, row: Any, project_id: object, quality: str) -> Source | None:
        """The ready whole render *row* of the same video at *quality* a splice keeps the frames of; ``None`` else."""
        from app.models.video_studio import JOB_READY
        from app.services.video_studio import render_checks

        if row is None or row.status != JOB_READY or row.scene_id is not None or row.project_id != project_id:
            return None
        if row.quality != quality:
            return None
        drawing = row.drawing if isinstance(row.drawing, dict) else {}
        checks = row.checks if isinstance(row.checks, dict) else {}
        takes = drawing.get("takes") if isinstance(drawing.get("takes"), dict) else {}
        # Its crops are its own files: the splice keeps what the inserts measured, never a file it would not own.
        inserts = [
            {key: value for key, value in report.items() if key != "crops" and key not in render_checks.INSERT_STRIPS}
            for report in checks.get("inserts") or []
            if isinstance(report, dict)
        ]
        return cls(
            str(row.id),
            str(row.file_id) if row.file_id else None,
            str(drawing["encoded_as"]) if drawing.get("encoded_as") else None,
            {str(sid): dict(take) for sid, take in takes.items() if isinstance(take, dict)},
            dict(checks["frames"]) if isinstance(checks.get("frames"), dict) else None,
            inserts,
            dict(checks["cuts"]) if isinstance(checks.get("cuts"), dict) else None,
        )


@dataclass(frozen=True)
class Rechecked:
    """What a re-check of one scene measured (:func:`recheck`): spoken again now, never a record replayed."""

    scene_id: str
    #: It now passes at the video's speed in its own voice: the splice places it.
    cleared: bool
    video_speed: float
    #: The fallback take it still needs (``voice_check.FallbackTake``), when it passed only so - measured now.
    still: Any = None
    #: Why it could not be heard, or why it failed every take.
    reason: str = ""
    #: The share of its words heard as written in its take at the video's speed - the one placed when it passes, the
    #: best otherwise (``TranscriptCheck.heard_share``); ``None``: none was heard.
    share: float | None = None
    #: What spoke it: ``tts-1-hd, voice brand``.
    spoken_as: str = ""
    #: Spoken and heard again: ``False`` when nothing could speak or hear it.
    measured: bool = True

    def said(self) -> str:
        """``scene `s2` spoken again at 1 on tts-1-hd, voice brand, heard 100% of its words as written: it passes -
        the fallback take is replaced``."""
        if not self.measured:
            return f"scene `{self.scene_id}` was not re-checked: {self.reason}"
        spoken = f"scene `{self.scene_id}` spoken again at {self.video_speed:g}" + (
            f" on {self.spoken_as}" if self.spoken_as else ""
        )
        if self.share is not None:
            spoken += f", heard {self.share:.0%} of its words as written" + ("" if self.cleared else " at best")
        if self.cleared:
            return f"{spoken}: it passes - the fallback take is replaced"
        if self.still is not None:
            take = self.still
            return (
                f"{spoken}: it fails at that speed - the fallback take is kept ({take.take()}: {take.why()}, "
                f"measured now - {take.fix()} would let it pass at its speed)"
            )
        return f"{spoken}: it fails every take ({self.reason}) - the fallback take is kept"


def line_speeds(effective: dict) -> dict[str, float]:
    """``scene id -> the speed its narration is said at`` (``voice.line_speed``: its scene's own, else the video's)."""
    from app.services.video_studio import voice
    from app.services.video_studio.spec import parse_video_spec

    spec = parse_video_spec(effective)
    return {line.scene_id: voice.line_speed(line, spec.voiceover) for line in voice.lines_of(spec)}


def recorded(
    drawing: Mapping, checks: Mapping | None, speeds: Mapping[str, float], records: Mapping[str, Mapping] | None = None
) -> dict[str, tuple[Any, str | None]]:
    """``scene id -> (its fallback take, the fallback digest it was recorded under)`` of a ready render.

    An older render recorded none: its voice summaries say the scenes it played slower or in another
    voice - never under which pipeline, so their digest is ``None`` (moved) -, and never what the takes at the video's
    speed heard wrong (a summary counts its takes, ``SpokenSegment.summary``). *records* (``scene id -> its check
    record`` in the voice cache, :func:`records_of`) say it when they still place that take; the summary otherwise."""
    from app.services.video_studio import voice_check

    kept = drawing.get("fallbacks")
    if isinstance(kept, dict):
        found = {}
        for sid, entry in kept.items():
            take = voice_check.FallbackTake.of(str(sid), entry)
            if take is not None:
                found[str(sid)] = (take, str(entry.get("digest") or "") or None)
        return found
    found = {}
    for summary in (checks or {}).get("voice") or []:
        if not isinstance(summary, dict) or not summary.get("scene_id") or summary.get("status") != voice_check.PASSED:
            continue
        sid = str(summary["scene_id"])
        if voice_check.fallback_kind(summary) is None:
            continue
        record = (records or {}).get(sid)
        source = record if isinstance(record, Mapping) and voice_check.fallback_kind(record) is not None else summary
        take = voice_check.FallbackTake.of_record(sid, source, speeds.get(sid, 1.0))
        if take is not None:
            found[sid] = (take, None)
    return found


async def records_of(db: Any, user: Any, effective: dict, lexicon: Any = None) -> dict[str, dict]:
    """``scene id -> its last check record`` in the voice cache of each narration of *effective* as the render would
    speak it (``reuse_index.voice_digests``): what an older render recorded nothing of. Empty without a
    voice, or when the cache cannot be read."""
    from app.services.video_studio import reuse_index, voice_check
    from app.services.video_studio.spec import parse_video_spec

    spec = parse_video_spec(effective)
    try:
        digests = reuse_index.voice_digests(await reuse_index.voice_target(db, user, spec), spec, lexicon)
    except Exception as exc:  # no voice to read the cache under: the summaries alone say the takes
        logger.debug("Voice digests not read for the re-check offer: %s", format_exception_message(exc))
        return {}
    found = {}
    for sid, digest in digests.items():
        try:
            record = await voice_check.load_record(digest)
        except Exception as exc:  # a record that cannot be read is a record never written
            logger.debug("Check record of scene %s not read: %s", sid, format_exception_message(exc))
            continue
        if isinstance(record, dict):
            found[sid] = record
    return found


async def fallback_scenes(hearing: Hearing) -> list[str]:
    """The scenes of *hearing*'s video whose narration the voice cache's last check placed with a fallback take - a
    slower take, a term set apart, another voice - as its speaker says it now (the digest a render reads, at the video's
    speed): what ``render_video(recheck: true)`` measures again before it starts. Empty when nothing can speak them."""
    from app.services.video_studio import voice, voice_check
    from app.services.video_studio.spec import parse_video_spec

    if hearing.speaker is None:
        return []
    spec = parse_video_spec(hearing.effective)
    found = []
    for line in voice.lines_of(spec):
        try:
            record = await voice_check.load_record(
                voice.digest_of(hearing.speaker.target, line, spec.voiceover, hearing.speaker.words)
            )
        except Exception as exc:  # a record that cannot be read is a record never written
            logger.debug("Check record of scene %s not read: %s", line.scene_id, format_exception_message(exc))
            continue
        if isinstance(record, dict) and voice_check.fallback_kind(record) is not None:
            found.append(line.scene_id)
    return found


def _moved(drawing: Mapping, marks: Any, fallbacks: Mapping[str, tuple[Any, str | None]]) -> list[Recheck]:
    """The scenes of *fallbacks* whose fallback digest moved and that no re-check settled at the digest they have now."""
    from app.services.video_studio import render_checks, voice_check

    settled = drawing.get("rechecked") if isinstance(drawing.get("rechecked"), dict) else {}
    then = drawing.get("said") if isinstance(drawing.get("said"), dict) else {}
    found = []
    for sid, (take, digest) in fallbacks.items():
        if sid not in marks.said:
            continue
        now = render_checks.fallback_digest(marks.said[sid])
        if digest == now or settled.get(sid) == now:
            continue
        lexicon = sid in then and then[sid] != marks.said[sid]
        why = LEXICON_MOVED if lexicon and take.pipeline == voice_check.pipeline_version() else PIPELINE_MOVED
        found.append(Recheck(sid, take, why, now))
    return found


def offered(
    drawing: Mapping,
    checks: Mapping | None,
    marks: Any,
    speeds: Mapping[str, float],
    records: Mapping[str, Mapping] | None = None,
) -> list[Recheck] | None:
    """The scenes of a ready render, recorded as *drawing* and *checks*, a re-check is offered for against the
    revision's *marks* (``render_checks.SceneDigests``) - ``[]`` when it covers the revision as it is, ``None`` when it
    does not and no re-check would make it.

    Everything drawn and heard the same: the scenes playing a fallback take whose digest moved since (the pipeline).
    Otherwise only what the voice alone decides may have moved - the frames' inputs but for the narrations' lengths,
    the sound beside the voice and the set of narrations the same -, every narration whose lexicon moved playing a
    fallback take: those scenes, with any other whose pipeline moved. A scene re-checked to no avail at the digest it
    has now is never offered again (:func:`settle`). *records*: the voice cache's check records of a render that
    recorded no fallback (:func:`records_of`).
    """
    fallbacks = recorded(drawing, checks, speeds, records)
    if drawing.get("scenes") == marks.drawn and drawing.get("heard") == marks.heard:
        return _moved(drawing, marks, fallbacks)
    picture, beside, said = drawing.get("picture"), drawing.get("beside"), drawing.get("said")
    if not picture or not beside or not isinstance(said, dict):
        return None
    if picture != marks.picture or beside != marks.beside or set(said) != set(marks.said):
        return None
    changed = {sid for sid, digest in marks.said.items() if said.get(sid) != digest}
    if not changed or not changed <= set(fallbacks):
        return None
    return _moved(drawing, marks, fallbacks) or None


async def played(
    spoken: Sequence[Any], placed: Mapping[str, str], digests: Mapping[str, str], marks: Any, speeds: Mapping[str, float]
) -> dict:
    """What a whole render records of its narrations (``VideoRender.drawing``): ``takes`` - each narration *placed*
    (``scene id -> its WAV``) with its take in the voice cache and the digest its record is kept under (*digests*,
    ``reuse_index.voice_digests``) - and ``fallbacks`` - each scene playing a fallback take, with its fix and its
    reuse digest (``render_checks.fallback_digest`` of its ``SceneDigests.said`` in *marks*)."""
    from app.services.video_studio import render_checks, voice_check

    takes: dict[str, dict] = {}
    fallbacks: dict[str, dict] = {}
    for segment in spoken:
        sid, take = segment.scene_id, segment.placed
        if take is None or sid not in placed or not take.object_name:
            continue
        takes[sid] = {
            "take": take.object_name,
            "digest": digests.get(sid),
            "voice": take.voice,
            "speed": take.speed,
            "reframed": list(take.reframed),
            "mechanism": take.mechanism,
        }
        summary = segment.summary()
        if voice_check.fallback_kind(summary) is None:
            continue
        record = None
        if digests.get(sid):
            try:
                record = await voice_check.load_record(digests[sid])
            except Exception as exc:  # what the takes tried is said when it can be read; the take is a fallback anyway
                logger.debug("Check record of scene %s not read: %s", sid, format_exception_message(exc))
        source = record if record and voice_check.fallback_kind(record) is not None else summary
        found = voice_check.FallbackTake.of_record(sid, source, speeds.get(sid, 1.0))
        if found is None:
            continue
        fallbacks[sid] = {
            **found.as_dict(),
            "pipeline": voice_check.pipeline_version(),
            "digest": render_checks.fallback_digest(marks.said.get(sid, "")),
        }
        if segment.replayed_from:
            # Placed from the record that heard it: its figures are that check's, never this render's.
            fallbacks[sid]["replayed_from"] = segment.replayed_from
    return {"takes": takes, "fallbacks": fallbacks}


def fallback_notes(fallbacks: Mapping[str, dict]) -> list[str]:
    """What a whole render says of each scene it plays with a fallback take: the fact and the fix that clears it."""
    from app.services.video_studio import voice_check

    notes = []
    for sid, entry in fallbacks.items():
        take = voice_check.FallbackTake.of(sid, entry)
        if take is not None:
            replayed = entry.get("replayed_from")
            measured = (
                f" (measured by the check of {replayed} that placed this take, not heard again by this render: "
                "render_video(recheck: true) measures it now)"
                if replayed
                else ""
            )
            notes.append(
                f"Fallback take: {take.fact()}{measured}; once fixed, render_video re-checks that narration alone, "
                "without drawing a frame where its new take draws the same."
            )
    return notes


async def settle(db: Any, render_id: str, digests: Mapping[str, str]) -> None:
    """Keep on the ready render *render_id* the scenes a re-check could not clear, each at its fallback digest now
    (``drawing["rechecked"]``): never offered again for that digest."""
    import uuid

    from app.models.video_studio import VideoRender

    if not digests:
        return
    row = await db.get(VideoRender, uuid.UUID(str(render_id)), with_for_update=True)
    if row is None:
        return
    drawing = dict(row.drawing) if isinstance(row.drawing, dict) else {}
    settled = dict(drawing.get("rechecked") or {}) if isinstance(drawing.get("rechecked"), dict) else {}
    row.drawing = {**drawing, "rechecked": {**settled, **{str(sid): str(digest) for sid, digest in digests.items()}}}


@dataclass(frozen=True)
class Hearing:
    """What re-checks a video's narrations, read on a session (:func:`hearing_for`) and used once it is closed."""

    speaker: Any
    effective: dict
    #: Why nothing can hear them back (no speech model, no speech-to-text).
    unavailable: str | None = None


async def hearing_for(db: Any, user: Any, effective: dict, *, origin: str) -> Hearing:
    """The speaker that re-checks *effective*'s narrations as *user* (``voice_check.Speaker``): the video's voice, the
    speech-to-text the person may use and the video's lexicon - the platform's entries under its own."""
    from app.llm.transcription import resolve_transcriber
    from app.services.video_studio import lexicon_store, voice, voice_check
    from app.services.video_studio.spec import parse_video_spec

    spec = parse_video_spec(effective)
    try:
        target = voice.detached(await voice.resolve(db, user.id, spec.voiceover))
    except voice.VoiceUnavailableError as exc:
        return Hearing(None, effective, str(exc))
    try:
        transcriber = await resolve_transcriber(db, user.id)
    except Exception as exc:
        return Hearing(None, effective, f"no speech-to-text can hear it back ({format_exception_message(exc)[:200]})")
    lexicon = await lexicon_store.for_voiceover(db, spec.voiceover)
    return Hearing(voice_check.Speaker(target, spec.voiceover, transcriber, None, lexicon=lexicon, origin=origin), effective)


def _spoken_as(speaker: Any, voiceover: Any) -> str:
    """``tts-1-hd, voice brand``: the speech model and the voice a re-check speaks a narration with."""
    model = getattr(speaker.target, "model", None) or "the speech model"
    named = getattr(voiceover, "voice", None) or getattr(speaker.target, "voice", None)
    return f"{model}, voice {named}" if named else f"{model}, its default voice"


def _share_at_speed(spoken: Any, cleared: bool) -> float | None:
    """The share of words heard as written in *spoken*'s take at the video's speed: the one placed when it passes
    there (*cleared*), the best of the video's own voice otherwise; ``None`` when none was heard."""
    from app.services.video_studio import voice_check

    placed = spoken.placed
    if cleared and placed is not None and placed.check is not None:
        return placed.check.heard_share
    own = [
        attempt.check.heard_share
        for attempt in spoken.attempts
        if attempt.kind in voice_check.OWN_VOICE and attempt.speed is None and attempt.check is not None
    ]
    return max(own, default=None)


async def recheck(hearing: Hearing, scenes: Sequence[str]) -> list[Rechecked]:
    """Each of *scenes* measured again (``voice_check.speak_checked`` with ``fresh``): spoken anew at the video's
    speed past the voice cache, with the video's voice, model and lexicon as they are now, and heard back with the
    transcript check ``preview_voiceover`` runs (``voice_check.hear_back``) - never a check record's figure replayed.
    The record it leaves is what the next render places: a narration that now passes there plays at that speed."""
    from app.services.video_studio import voice, voice_check
    from app.services.video_studio.spec import parse_video_spec

    spec = parse_video_spec(hearing.effective)
    lines = {line.scene_id: line for line in voice.lines_of(spec)}
    speeds = line_speeds(hearing.effective)
    if hearing.speaker is None:
        why = hearing.unavailable or "nothing can speak it"
        return [Rechecked(sid, False, speeds.get(sid, 1.0), reason=why, measured=False) for sid in scenes]
    spoken_as = _spoken_as(hearing.speaker, spec.voiceover)
    found = []
    with tempfile.TemporaryDirectory(prefix="xtm-voice-recheck-") as workdir:
        for sid in scenes:
            line, speed = lines.get(sid), speeds.get(sid, 1.0)
            if line is None:
                found.append(Rechecked(sid, False, speed, reason="it no longer has a narration", measured=False))
                continue
            try:
                spoken = await voice_check.speak_checked(hearing.speaker, line, workdir=workdir, fresh=True)
            except Exception as exc:  # a re-check that cannot run leaves the render as it is
                logger.warning("Re-check of scene %s not run: %s", sid, format_exception_message(exc))
                why = f"it could not be spoken ({format_exception_message(exc)[:160]})"
                found.append(Rechecked(sid, False, speed, reason=why, measured=False))
                continue
            if spoken.status == voice_check.UNCHECKED:
                why = spoken.reason or "no speech-to-text heard it back"
                found.append(Rechecked(sid, False, speed, reason=why, spoken_as=spoken_as, measured=False))
                continue
            summary = spoken.summary()
            cleared = spoken.status == voice_check.PASSED and voice_check.fallback_kind(summary) is None
            share = _share_at_speed(spoken, cleared)
            if cleared:
                found.append(Rechecked(sid, True, speed, share=share, spoken_as=spoken_as))
            elif spoken.status == voice_check.PASSED:
                # The record this very measurement just wrote: what its takes at the video's speed heard wrong.
                digest = voice.digest_of(hearing.speaker.target, line, spec.voiceover, hearing.speaker.words)
                record = await voice_check.load_record(digest)
                source = record if record and voice_check.fallback_kind(record) is not None else summary
                still = voice_check.FallbackTake.of_record(sid, source, speed)
                found.append(Rechecked(sid, False, speed, still=still, share=share, spoken_as=spoken_as))
            else:
                reason = (spoken.reason or "it failed its transcript check")[:300]
                found.append(Rechecked(sid, False, speed, reason=reason, share=share, spoken_as=spoken_as))
    return found


def offer_line(render_id: str, revision: int, rechecks: Sequence[Recheck]) -> str:
    """``Render `X` covers the picture of revision N; scene `s2` plays a 0.85 take, the voice check changed since:
    re-check it? ...`` - what ``render_video`` answers instead of "already covers"."""
    one = len(rechecks) == 1
    scenes = "; ".join(recheck.said() for recheck in rechecks)
    return (
        f"Render `{render_id}` covers the picture of revision {revision}; {scenes}: re-check {'it' if one else 'them'}? "
        f"Ask the user; render_video(recheck: true) speaks {'that narration' if one else 'those narrations'} alone "
        f"again with the voice check as it is now, hears {'it' if one else 'them'} back and, where one now passes at "
        "the video's speed, splices its sound into a new render without drawing a frame (drawn again only where the "
        "new take changes what is drawn). Nothing was queued; render_video(force: true) renders everything again."
    )


#: How a re-check hears a narration, said with every result: the same check as preview_voiceover, and what differs.
HOW_HEARD = (
    "each spoken again past the voice cache with the video's voice, speed and lexicon, and heard back with the "
    "transcript check preview_voiceover runs (same speech-to-text, normalisation and lexicon); a render speaks a "
    "narration with the narrations around it as context, which a preview of the line alone does not"
)


def outcome_line(render_id: str, outcomes: Sequence[Rechecked], spliced: str | None) -> str:
    """What ``render_video(recheck: true)`` says of each scene re-checked, and of the splice it queued (*spliced*)."""
    said = "; ".join(outcome.said() for outcome in outcomes)
    if spliced:
        takes = "takes" if sum(outcome.cleared for outcome in outcomes) > 1 else "take"
        return (
            f"Re-checked ({HOW_HEARD}): {said}. Render `{spliced}` splices the new {takes} into render `{render_id}`: "
            "every other narration as it plays there, its frames kept when the new take draws the same - its notes "
            "say whether any frame was drawn."
        )
    return (
        f"Re-checked ({HOW_HEARD}): {said}. Render `{render_id}` stays the video's render and nothing was queued; it "
        "is no longer offered for these scenes until they change."
    )


#: What ``render_video(recheck: true)`` started once it measured the narrations (:func:`measured_line`).
QUEUED, IN_FLIGHT, COVERED = "queued", "in_flight", "covered"


def measured_line(
    outcomes: Sequence[Rechecked], render_id: str, *, state: str = QUEUED, unavailable: str | None = None
) -> str:
    """What ``render_video(recheck: true)`` says of the narrations it measured before a render it did not splice:
    ``No re-check performed`` when none was (*unavailable*: why nothing could speak or hear them; else the voice
    cache places no fallback take), each result otherwise, and what render *render_id* plays of them - *state*:
    :data:`QUEUED` (it places each as heard now), :data:`IN_FLIGHT` (already being made: the takes it spoke),
    :data:`COVERED` (the ready render covering the revision, none of whose fallback takes now passes)."""
    if unavailable:
        return f"No re-check performed: {unavailable}; the render places each narration as its last check did."
    if not outcomes:
        return (
            "No re-check performed: no narration of this video plays a fallback take - the voice cache's last check "
            "of each placed the video's own voice at its speed - so none was spoken again."
        )
    said = "; ".join(outcome.said() for outcome in outcomes)
    if state == IN_FLIGHT:
        plays = (
            f"Render `{render_id}` was already being made and plays the takes it spoke; the next render places each "
            "narration as heard now."
        )
    elif state == COVERED:
        plays = (
            f"Render `{render_id}` stays the video's render: no narration it plays with a fallback take passes at "
            "the video's speed now."
        )
    else:
        plays = (
            f"Render `{render_id}` places each narration as heard now: one that passes plays at the video's speed, "
            "with no fallback take and no fallback note."
        )
    return f"Re-checked before rendering ({HOW_HEARD}): {said}. {plays}"


def splice_note(
    source: str, drawn: int, kept: bool, spoken: Sequence[Any], scenes: Sequence[str], speeds: Mapping[str, float]
) -> str:
    """What a splice says first: whether it kept the frames of render *source*, and what each re-checked
    narration now plays at."""
    heard = []
    for segment in spoken:
        if segment.scene_id not in scenes:
            continue
        take = segment.placed
        speed = speeds.get(segment.scene_id, 1.0)
        if take is not None and take.speed is not None:
            heard.append(f"scene `{segment.scene_id}` still plays a {take.speed:g} take")
        else:
            heard.append(f"scene `{segment.scene_id}` plays its narration at the video's speed ({speed:g})")
    narrations = "; ".join(heard) or "no narration re-checked"
    if kept:
        return (
            f"Spliced into render `{source}`: {narrations}. None of its {drawn:,} frames was drawn - they are that "
            "render's, byte for byte, with its picture checks; every other narration plays the take it played there, "
            "and the sound was mixed and heard again."
        )
    return (
        f"Re-checked into a new render of render `{source}`'s revision: {narrations}. Its frames were drawn again: the "
        "new take changes what is drawn (a scene's length or its burned-in captions follow its narration), or that "
        "render's frames could not be read."
    )
