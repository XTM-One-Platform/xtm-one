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

"""Music for videos: the built-in library, AI tracks and the bed a video plays.

Three sources, one bed:

* **built-in tracks** (``score.STYLES``): royalty-free scores the platform
  composes for each video - its length, its scene changes, its narration -
  with no provider at all;
* **AI tracks** (``VideoMusic``): composed by a music model
  (``router.get_music_provider``: Eleven Music, Lyria) in the ``video_music``
  job, at the length asked (the video's, when a video asks), instrumental
  unless asked otherwise, reusable by any video of their owner and generated
  again in place when they failed (:func:`restart_music`);
* an **uploaded** sound file.

Whatever the source, the video plays a *bed* (:class:`Bed`): exactly its
length, at the voice's loudness (``encoder.prepare_bed``), from an offset,
looped with crossfades or ending with the track's own end. The render
prepares it next to the mix; the editor's preview and the library's listen
button play the same bed, prepared once per digest of what decides it and
kept in a bounded local cache (:func:`preview_bed`).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import shutil
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from app.core.utils import format_exception_message
from app.llm.music import MusicGenerationError, MusicProviderBusyError, MusicRequest, MusicResult, clamp_seconds

logger = logging.getLogger(__name__)

#: How long an audition of a built-in track lasts.
AUDITION_SECONDS = 32.0
#: The length an AI track is asked for beyond the video's planned one: the
#: pacing of the real voice moves the end, and the track's own fade must land
#: after the outro, never before it - the bed fades on the last frame.
FIT_MARGIN_SECONDS = 8.0
#: The most a call may ask beyond the video's length (``margin_seconds``).
MAX_FIT_MARGIN_SECONDS = 60.0
#: A video longer than its looping AI track by more than this outgrew it: a warning at save.
OUTGROWN_SECONDS = 2.0
#: What an AI track generated again may change; the rest is the track as it was asked.
RETRY_FIELDS: tuple[str, ...] = (
    "prompt",
    "model",
    "provider_id",
    "duration_seconds",
    "instrumental",
    "negative_prompt",
)
#: How many times one track is generated again: each attempt is a paid generation.
MAX_RETRIES = 5
#: The params key holding a ready track's previous file while it is composed again at a new length:
#: deleted once the new composition ends (``runners.run_music``).
REPLACED_MEDIA = "replaced_media"
#: An extended refit keeps the take and composes again only its last seconds - its own ending - as the
#: continuation of the :data:`EXTEND_REFERENCE_SECONDS` before them, crossfaded in over its own audio past the cut.
EXTEND_TAIL_SECONDS = 24.0
EXTEND_REFERENCE_SECONDS = 30.0
EXTEND_CROSSFADE_SECONDS = 2.0
#: The least of a take an extension keeps.
EXTEND_MIN_KEPT_SECONDS = 10.0
#: The params key holding the take an extended refit keeps (``{file_id, seconds, duration_ms, cut}``): its file is
#: deleted once the extended take is made, and plays again if the extension ends without one (:func:`kept_take`).
EXTENDED_FROM = "extended_from"
#: Why the last extension of a track was not made, said with it while its kept take plays (:func:`kept_take`).
EXTEND_FAILED = "extend_failed"
#: A padded refit extends the take with no model call: the second of it still at its level before its own
#: ending (:data:`PAD_ENDING_DB` under its level) is held, its spectrum frozen (:func:`padded_samples`), crossfaded in
#: over :data:`PAD_CROSSFADE_SECONDS` and faded out over the last :data:`PAD_FADE_SECONDS` - at most
#: :data:`PAD_MOST_SECONDS` past the take, past which a held chord is heard as a drone. The save proposes it for a
#: play-once track ending less than :data:`PAD_PROPOSED_SECONDS` before the video.
PAD_MOST_SECONDS = 8.0
PAD_PROPOSED_SECONDS = 3.0
PAD_HOLD_SECONDS = 1.0
PAD_ENDING_DB = 12.0
PAD_CROSSFADE_SECONDS = 1.0
PAD_FADE_SECONDS = 3.0
#: What the answer says a padded take is made with, where an extension names its model.
PADDED_WITH = "the take itself, no model call"
#: The bed cache's directory and size, on the API's own disk.
_CACHE_DIR = Path(tempfile.gettempdir()) / "xtm-music-beds"
_CACHE_BYTES = 512 * 1024 * 1024
#: Beds prepared at once per process (a built-in score is seconds of CPU).
_PREPARING = asyncio.Semaphore(2)
_LOCKS: dict[str, asyncio.Lock] = {}
#: The version of the bed recipe: a cached bed made by another one is not reused (2: a track played once is never
#: looped, it fades out on its own end).
BED_VERSION = 2


class MusicError(RuntimeError):
    """An AI track that could not be asked for or made; ``str()`` is safe to show."""


# ── The library ───────────────────────────────────────────────────────────


def library() -> list[dict]:
    """The built-in tracks: id, name, mood, genre, tempo, what each is and the kinds of video it suits."""
    from app.services.video_studio.score import STYLES

    return [style.summary() for style in STYLES]


# ── AI tracks ─────────────────────────────────────────────────────────────


async def resolve(db: Any, user_id: object, *, model: str | None = None, provider_id: object = None):
    """``(provider, model, config)`` a track is composed with, or :class:`MusicError`."""
    from app.llm.router import get_music_provider

    try:
        return await get_music_provider(
            db, user_id=user_id, provider_id=provider_id, model=(model or "").strip() or None
        )
    except ValueError as exc:
        raise MusicError(str(exc)) from None


async def resolve_continuing(
    db: Any, user_id: object, *, model: str | None = None, provider_id: object = None, asked: bool = False
):
    """``(provider, model, config)`` that composes the continuation of a clip (``continues_music``).

    *model* on *provider_id* when it does. Else, unless the person *asked* for
    that one, the first music model they may use that does
    (``router.list_music_models_for``): the take's own model may not - one
    of the take's own provider first, then the others'.
    :class:`MusicError` when none does.
    """
    from app.llm.router import list_music_models_for

    found = None
    try:
        found = await resolve(db, user_id, model=model, provider_id=provider_id)
    except MusicError:
        if asked:
            raise
    if found is not None and found[0].continues_music(found[1]):
        return found
    if asked and found is not None:
        raise MusicError(
            f"{found[1]} on {provider_label(found[2])} does not continue a clip of the take: name a music model "
            "video_studio_capabilities marks continues: true, or refit without extend."
        )
    own = str(found[2].id) if found is not None else str(provider_id or "")
    entries = [entry for entry in await list_music_models_for(db, user_id=user_id) if entry.get("continues")]
    if entries:
        chosen = sorted(entries, key=lambda entry: str(entry.get("provider_id")) != own)[0]
        return await resolve(db, user_id, model=chosen["model"], provider_id=chosen["provider_id"])
    raise MusicError(
        "No music model you may use continues a clip of the take (video_studio_capabilities marks those continues: "
        "true): refit without extend composes the whole track again."
    )


def provider_label(config: Any) -> str:
    """What the person calls the provider a track is composed on: its display name, else its type."""
    return getattr(config, "display_name", None) or config.provider


async def credits_refusal(provider: Any, model: str, config: Any) -> str | None:
    """``music_v2_5 on ElevenLabs: its account has no credits left`` when *provider*'s account cannot pay for a
    composition with *model* now (``BaseLLMProvider.music_credits``), read before the track is queued: the call
    refuses it, never a job failing on its quota after the call answered. ``None`` when it can, or does not say."""
    left = await provider.music_credits(model)
    if left is None or left > 0:
        return None
    return f"{model} on {provider_label(config)}: its account has no credits left"


def build_request(
    *, model: str, prompt: str, duration_seconds: float, instrumental: bool = True, negative_prompt: str | None = None
) -> MusicRequest:
    return MusicRequest(
        model=model,
        prompt=prompt.strip()[:2000],
        duration_seconds=clamp_seconds(duration_seconds),
        instrumental=bool(instrumental),
        negative_prompt=(negative_prompt or "").strip()[:500] or None,
    )


def fit_margin(value: Any) -> float:
    """The ``margin_seconds`` a call asks (0 to :data:`MAX_FIT_MARGIN_SECONDS`), :data:`FIT_MARGIN_SECONDS` unset."""
    if value in (None, ""):
        return FIT_MARGIN_SECONDS
    try:
        margin = float(value)
    except (TypeError, ValueError):
        raise MusicError(f"margin_seconds is a number of seconds from 0 to {MAX_FIT_MARGIN_SECONDS:g}.") from None
    if not math.isfinite(margin) or not 0.0 <= margin <= MAX_FIT_MARGIN_SECONDS:
        raise MusicError(f"margin_seconds is a number of seconds from 0 to {MAX_FIT_MARGIN_SECONDS:g}.")
    return margin


def fitted_seconds(planned: float, margin: float = FIT_MARGIN_SECONDS) -> int:
    """The length an AI track is asked for to play under a video planned at *planned* seconds."""
    return math.ceil(clamp_seconds(planned + margin))


def video_seconds(planned: float, rendered: float | None = None) -> float:
    """How long a video's AI track must play: its *planned* length, or its current render's when longer.

    A render measures the voice the plan only estimates, so a render of the
    video's current revision that came out longer is the length to cover
    (``projects.rendered_seconds``).
    """
    return max(planned, rendered) if rendered else planned


def _length_words(planned: float, rendered: float | None) -> str:
    if rendered and rendered > planned:
        return f"the video's {rendered:.0f} s as its current render lasts (planned at {planned:.0f} s)"
    return f"the video's {planned:.0f} s planned now"


def refit_seconds(
    planned: float, asked: Any = None, margin: float = FIT_MARGIN_SECONDS, *, rendered: float | None = None
) -> tuple[int, str]:
    """``(seconds, what the answer says)`` a video's AI track is composed again at.

    A refit fits the track to the video: never shorter than its length
    (:func:`video_seconds`: the plan, or the current render's when longer)
    plus *margin*, an *asked* length included, so the track's own fade
    lands after the outro.
    """
    length = video_seconds(planned, rendered)
    fitted = fitted_seconds(length, margin)
    try:
        wanted = math.ceil(clamp_seconds(float(asked))) if asked not in (None, "") else None
    except (TypeError, ValueError):
        wanted = None
    if wanted is not None and wanted > fitted:
        return wanted, f"Composed at {wanted} s, as asked: {_length_words(planned, rendered)}."
    lifted = f" (duration_seconds {wanted} s was shorter)" if wanted is not None and wanted < fitted else ""
    return fitted, (
        f"Composed at {fitted} s: {_length_words(planned, rendered)} plus {margin:g} s{lifted}, so the "
        "track's own end lands after the outro and the bed fades on the last frame."
    )


async def start_music(
    db: Any,
    user: Any,
    *,
    prompt: str,
    duration_seconds: float = 60.0,
    model: str | None = None,
    provider_id: object = None,
    instrumental: bool = True,
    negative_prompt: str | None = None,
    conversation_id: object = None,
    name: str | None = None,
):
    """Queue an AI track for *user*: ``(row, provider label)``, flushed and enqueued, not committed.

    The one start of a track - the agent tool, the editor and a video saved
    with a music prompt alike. Raises :class:`MusicError` with a message the
    person reads.
    """
    from app.models.video_studio import VideoMusic
    from app.services.video_studio import jobs

    if not (prompt or "").strip():
        raise MusicError("A track needs a prompt describing it (genre, mood, instruments, tempo, how it evolves).")
    provider, chosen_model, config = await resolve(db, user.id, model=model, provider_id=provider_id)
    if refused := await credits_refusal(provider, chosen_model, config):
        raise MusicError(f"No track was started: {refused} - add credits there, or name another music model.")
    request = build_request(
        model=chosen_model,
        prompt=prompt,
        duration_seconds=duration_seconds,
        instrumental=instrumental,
        negative_prompt=negative_prompt,
    )
    row = VideoMusic(
        user_id=user.id,
        conversation_id=conversation_id,
        name=(name or "").strip()[:200] or None,
        provider=config.provider,
        model=request.model,
        prompt=request.prompt,
        params={
            "provider_id": str(config.id),
            "duration_seconds": request.duration_seconds,
            "instrumental": request.instrumental,
            "negative_prompt": request.negative_prompt,
        },
    )
    db.add(row)
    await db.flush()
    await jobs.enqueue(db, "music", row, owed_to=row.conversation_id)
    return row, provider_label(config)


def extension(row: Any, seconds: float) -> dict:
    """What an extended refit keeps of *row*'s take to make it *seconds* long: ``{file_id, seconds,
    duration_ms, cut}`` - its file, its length and the second its tail is composed again from,
    :data:`EXTEND_TAIL_SECONDS` before its end. :class:`MusicError` for no ready take, one too short to keep or one
    already that long."""
    from app.models.video_studio import JOB_READY

    if row.status != JOB_READY or not getattr(row, "file_id", None) or not getattr(row, "duration_ms", None):
        raise MusicError("Only a ready take is extended: refit without extend composes this track again.")
    take = row.duration_ms / 1000
    cut = round(take - EXTEND_TAIL_SECONDS, 3)
    if cut < EXTEND_MIN_KEPT_SECONDS:
        raise MusicError(
            f"The take lasts {take:.0f} s: too short to keep {EXTEND_MIN_KEPT_SECONDS:g} s of it and compose its last "
            f"{EXTEND_TAIL_SECONDS:g} s again - refit without extend."
        )
    if seconds <= take:
        raise MusicError(f"The take lasts {take:.0f} s, as long as the {seconds:.0f} s asked: nothing to extend.")
    return {"file_id": str(row.file_id), "seconds": take, "duration_ms": int(row.duration_ms), "cut": cut}


def pad_seconds(planned: float, rendered: float | None = None, margin: Any = None) -> int:
    """The length a padded refit holds a video's take to: the video's (:func:`video_seconds`) plus the
    *margin_seconds* asked, none unset - the pad's own fade then ends on the video's last frame."""
    extra = fit_margin(margin) if margin not in (None, "") else 0.0
    return math.ceil(clamp_seconds(video_seconds(planned, rendered) + extra))


def padding(row: Any, seconds: float) -> dict:
    """What a padded refit keeps of *row*'s take to hold it to *seconds*: :func:`extension`'s record, cut at the
    take's end and marked ``pad`` - nothing composed, its last chord held (:func:`padded_samples`). :class:`MusicError`
    for no ready take, one already that long, or one more than :data:`PAD_MOST_SECONDS` short."""
    from app.models.video_studio import JOB_READY

    if row.status != JOB_READY or not getattr(row, "file_id", None) or not getattr(row, "duration_ms", None):
        raise MusicError("Only a ready take is padded: refit without pad composes this track again.")
    take = row.duration_ms / 1000
    if seconds <= take:
        raise MusicError(f"The take lasts {take:.0f} s, as long as the {seconds:.0f} s asked: nothing to pad.")
    if seconds - take > PAD_MOST_SECONDS:
        raise MusicError(
            f"The take lasts {take:.1f} s, {seconds - take:.1f} s short of the {seconds:.0f} s asked: a chord held "
            f"longer than {PAD_MOST_SECONDS:g} s is heard as a drone - extend: true composes its ending again, refit "
            "without pad the whole track."
        )
    return {
        "file_id": str(row.file_id),
        "seconds": take,
        "duration_ms": int(row.duration_ms),
        "cut": round(take, 3),
        "pad": True,
    }


def pad_offer(take: float, seconds: float) -> str:
    """``generate_music(video_id, refit: true, pad: true) holds its last chord 1.2 s and fades, with no model call``
    when a take *take* s long is less than :data:`PAD_MOST_SECONDS` short of *seconds*; empty otherwise."""
    gap = seconds - take
    if not 0 < gap <= PAD_MOST_SECONDS:
        return ""
    return f"generate_music(video_id, refit: true, pad: true) holds its last chord {gap:.1f} s and fades, with no model call"


async def restart_music(
    db: Any,
    user: Any,
    row: Any,
    *,
    owed_to: Any = None,
    refit: bool = False,
    extend: bool = False,
    pad: bool = False,
    **changes: Any,
) -> str:
    """Generate a failed or cancelled track again, in place: every video using it keeps it.

    *refit* also composes a READY track again - at the length a video
    now needs: its file is kept until the new composition ends, then deleted.
    *extend* (with *refit*) keeps the take instead: only its tail is
    composed again, as the continuation of a clip of it, on a model that
    takes one (:func:`resolve_continuing`, :func:`extension`); the take plays
    again when the extension ends without a new one (:func:`kept_take`).
    *pad* (with *refit*) keeps the take and holds its last chord to the
    length asked with no model call (:func:`padding`, :func:`pad_take`): no
    provider, no credits, no attempt spent of :data:`MAX_RETRIES`.
    The account of the provider it is composed on is read first
    (:func:`credits_refusal`): one with no credits left refuses the call - an
    extension a few seconds long offered the pad instead (:func:`pad_offer`).
    The row is locked before its status is read (the editor and the assistant
    asking at once start one attempt). *owed_to* is the conversation of the
    chat turn asking (``jobs.enqueue``). Returns the provider label
    (:data:`PADDED_WITH` for a pad); raises :class:`MusicError`. Flushed and
    enqueued, not committed.
    """
    from app.models.video_studio import JOB_CANCELLED, JOB_FAILED, JOB_READY
    from app.services.video_studio import jobs

    if extend and not refit:
        raise MusicError("extend keeps a video's take and composes its tail again: it goes with refit: true.")
    if pad and not refit:
        raise MusicError("pad keeps a video's take and holds its last chord to the video's length: it goes with refit: true.")
    if pad and extend:
        raise MusicError("pad holds the take's last chord with no model, extend composes its ending again with one: pick one.")
    await db.refresh(row, with_for_update=True)
    allowed = (JOB_FAILED, JOB_CANCELLED, JOB_READY) if refit else (JOB_FAILED, JOB_CANCELLED)
    if row.status not in allowed:
        if refit:
            raise MusicError("This track is still being composed: wait for it (get_video_jobs), then refit it.")
        raise MusicError("Only a failed or cancelled track is generated again; this one is not.")
    params = dict(row.params or {})
    params.pop(EXTEND_FAILED, None)
    asked_seconds = float(changes.get("duration_seconds") or 0.0)
    if pad:
        params[EXTENDED_FROM] = padding(row, asked_seconds)
        attempt = int(params.get("attempt") or 0) + 1
        params.update(
            {"duration_seconds": clamp_seconds(asked_seconds), "attempt": attempt, "pads": int(params.get("pads") or 0) + 1}
        )
        params.pop("notes", None)
        _queue_again(row, params)
        await db.flush()
        await jobs.enqueue(db, "music", row, generation=attempt, owed_to=owed_to)
        return PADDED_WITH
    if extend:
        params[EXTENDED_FROM] = extension(row, asked_seconds)
    elif getattr(row, "file_id", None):
        params[REPLACED_MEDIA] = [*(params.get(REPLACED_MEDIA) or []), str(row.file_id)]
    if int(params.get("attempt") or 0) - int(params.get("pads") or 0) >= MAX_RETRIES:
        raise MusicError(
            f"This track was generated again {MAX_RETRIES} times: ask for a new one with another prompt or model."
        )
    prompt = (changes.get("prompt") or row.prompt or "").strip()
    if not prompt:
        raise MusicError("A track needs a prompt describing it (genre, mood, instruments, tempo, how it evolves).")
    wanted_model = (changes.get("model") or "").strip() or None
    wanted_provider = changes.get("provider_id") or (None if wanted_model else params.get("provider_id"))
    # A take a few seconds short is held to its length with no model: said where an extension cannot be made.
    offer = pad_offer(row.duration_ms / 1000, asked_seconds) if extend and row.duration_ms else ""
    padded = f"; or {offer}" if offer else ""
    if extend:
        try:
            provider, chosen_model, config = await resolve_continuing(
                db,
                user.id,
                model=wanted_model or row.model,
                provider_id=wanted_provider,
                asked=bool(wanted_model or changes.get("provider_id")),
            )
        except MusicError as exc:
            raise MusicError(f"{exc}{padded}") from None
    else:
        try:
            provider, chosen_model, config = await resolve(
                db, user.id, model=wanted_model or row.model, provider_id=wanted_provider
            )
        except MusicError:
            if wanted_model or changes.get("provider_id"):
                raise
            # The track's own model is gone (its provider removed): the platform's makes it.
            provider, chosen_model, config = await resolve(db, user.id)
    # Its account read before anything is queued: one with no credits refuses the call, the take as it was.
    if refused := await credits_refusal(provider, chosen_model, config):
        if extend and chosen_model != row.model:
            raise MusicError(
                f"The take's own {row.model} does not continue a clip, and the model that does, {refused}: add "
                f"credits there, or refit without extend (the whole track composed again with {row.model}){padded}."
            )
        raise MusicError(f"Nothing was composed again: {refused} - add credits there, or name another music model{padded}.")
    instrumental = changes.get("instrumental")
    request = build_request(
        model=chosen_model,
        prompt=prompt,
        duration_seconds=float(changes.get("duration_seconds") or params.get("duration_seconds") or 60),
        instrumental=params.get("instrumental", True) if instrumental is None else bool(instrumental),
        negative_prompt=changes.get("negative_prompt") or params.get("negative_prompt"),
    )
    attempt = int(params.get("attempt") or 0) + 1
    params.update(
        {
            "provider_id": str(config.id),
            "duration_seconds": request.duration_seconds,
            "instrumental": request.instrumental,
            "negative_prompt": request.negative_prompt,
            "attempt": attempt,
            "previous_error": row.error,
        }
    )
    params.pop("notes", None)
    params.pop("description", None)
    row.prompt = request.prompt
    row.model = request.model
    row.provider = config.provider
    _queue_again(row, params)
    await db.flush()
    await jobs.enqueue(db, "music", row, generation=attempt, owed_to=owed_to)
    return provider_label(config)


def _queue_again(row: Any, params: dict) -> None:
    """*row* pending again with *params*: nothing its last attempt's worker said still stands, and its file is
    no longer what plays - the take an extension or a pad keeps is in *params* (:data:`EXTENDED_FROM`)."""
    from app.models.video_studio import JOB_PENDING

    row.params = params
    row.status = JOB_PENDING
    row.progress = 0
    row.error = None
    row.finished_at = None
    row.stage = None
    row.progress_at = None
    row.file_id = None
    row.duration_ms = None


async def generate(provider: Any, request: MusicRequest) -> MusicResult:
    """The track's audio, from *provider*; every failure as :class:`MusicError`.

    A provider that cannot take the request now (:class:`MusicProviderBusyError`)
    is the one exception let through: the job asks again after a back-off.
    """
    try:
        return await provider.generate_music(request)
    except MusicProviderBusyError:
        raise
    except MusicGenerationError as exc:
        raise MusicError(str(exc)) from None
    except NotImplementedError:
        raise MusicError(f"{provider.provider_name} does not compose music.") from None
    except Exception as exc:
        raise MusicError(f"The music provider failed: {format_exception_message(exc)}") from exc


def held_files(row: Any) -> list[object]:
    """Every file *row* holds: the take it plays, the take an extension keeps and the takes a refit replaces -
    what deleting the track deletes."""
    params = dict(getattr(row, "params", None) or {})
    kept = params.get(EXTENDED_FROM) if isinstance(params.get(EXTENDED_FROM), dict) else {}
    found = [getattr(row, "file_id", None), kept.get("file_id"), *(params.get(REPLACED_MEDIA) or [])]
    return list(dict.fromkeys(str(file_id) for file_id in found if file_id))


def kept_take(params: dict, reason: str) -> dict | None:
    """What a track whose extension ended without a new take is marked with: ready on the take it kept,
    *reason* kept in *params* (:data:`EXTEND_FAILED`); ``None`` for a track that was extending nothing.

    Takes :data:`EXTENDED_FROM` out of *params*: the take's file plays again, never deleted.
    """
    from app.models.video_studio import JOB_READY

    take = params.pop(EXTENDED_FROM, None)
    if not isinstance(take, dict) or not take.get("file_id"):
        return None
    params[EXTEND_FAILED] = (f"the pad: {reason}" if take.get("pad") else str(reason))[:500]
    return {
        "status": JOB_READY,
        "file_id": uuid.UUID(str(take["file_id"])),
        "duration_ms": int(take.get("duration_ms") or round(float(take.get("seconds") or 0) * 1000)),
        "progress": 100,
        "stage": None,
        "error": None,
    }


async def _decoded(path: str, workdir: Path, name: str, *, rate: int, channels: int) -> Any:
    """*path*'s audio as ``float32`` samples ``[frames, channels]`` at *rate* and *channels*."""
    import numpy as np

    from app.services.video_studio import encoder

    raw = workdir / f"{name}.f32"
    await encoder.decode_pcm(path, str(raw), rate=rate, channels=channels)
    try:
        return np.fromfile(raw, dtype="<f4").reshape(-1, channels)
    finally:
        raw.unlink(missing_ok=True)


async def _written(samples: Any, out_path: str, workdir: Path, *, rate: int, codec: str) -> None:
    """*samples* ``[frames, channels]`` written as a WAV at *rate*, its samples as *codec*."""
    from app.services.video_studio import encoder

    raw = workdir / f"{Path(out_path).stem}.f32"
    samples.astype("<f4").tofile(raw)
    try:
        await encoder.encode_pcm(str(raw), out_path, rate=rate, channels=int(samples.shape[1]), codec=codec)
    finally:
        raw.unlink(missing_ok=True)


async def extend_take(
    provider: Any, request: MusicRequest, take_path: str, *, cut: float, workdir: Path
) -> tuple[str, MusicResult]:
    """The take at *take_path* with its tail from *cut* composed again: ``(the extended take's path, what the
    model answered)``.

    The :data:`EXTEND_REFERENCE_SECONDS` before *cut* go to the model as the
    clip *request* continues (``MusicRequest.continue_from``); its answer,
    from where its continuation starts (``MusicResult.continues_at``), comes
    in over the take's own audio past *cut* across
    :data:`EXTEND_CROSSFADE_SECONDS`, equal-power. Up to *cut* the take is
    kept sample for sample, at its own rate and channels: a 32-bit float WAV.
    """
    import numpy as np

    from app.llm.music import MusicReference
    from app.services.video_studio import encoder

    info = await encoder.probe(take_path)
    if not info.has_audio or not info.sample_rate or not info.channels:
        raise MusicError("The take's audio could not be read: refit without extend composes the track again.")
    rate, channels = info.sample_rate, info.channels
    take = await _decoded(take_path, workdir, "take", rate=rate, channels=channels)
    cut_at = min(round(cut * rate), len(take))
    clip = take[max(0, cut_at - round(EXTEND_REFERENCE_SECONDS * rate)) : cut_at]
    clip_path = str(workdir / "continue-from.wav")
    await _written(clip, clip_path, workdir, rate=rate, codec="pcm_s16le")
    request.continue_from = MusicReference(Path(clip_path).read_bytes(), "audio/wav", len(clip) / rate)
    made = await generate(provider, request)
    answer_path = workdir / "continuation.audio"
    answer_path.write_bytes(made.audio)
    answered = await _decoded(str(answer_path), workdir, "continuation", rate=rate, channels=channels)
    answer_path.unlink(missing_ok=True)
    after = answered[round(max(made.continues_at, 0.0) * rate) :]
    if not len(after):
        raise MusicError("The music model returned no continuation of the take.")
    fade = max(0, min(round(EXTEND_CROSSFADE_SECONDS * rate), len(take) - cut_at, len(after)))
    phase = (np.arange(fade, dtype=np.float64) + 0.5) / max(fade, 1) * (np.pi / 2)
    joined = take[cut_at : cut_at + fade] * np.cos(phase)[:, None] + after[:fade] * np.sin(phase)[:, None]
    extended = np.concatenate([take[:cut_at], joined.astype(np.float32), after[fade:]])
    out_path = str(workdir / "extended.wav")
    await _written(extended, out_path, workdir, rate=rate, codec="pcm_f32le")
    return out_path, made


def _frozen(source: Any, length: int) -> Any:
    """*length* frames of *source*'s sound held: its spectrum's magnitude, averaged over its frames, played with
    fresh phases frame after frame and overlapped - a chord without its attacks - at *source*'s loudness. The phases are
    shared by the channels (the held chord keeps its stereo image) and seeded (a pad made twice is the same)."""
    import numpy as np

    size = 1 << math.floor(math.log2(max(256, min(4096, len(source)))))
    hop = size // 4
    window = np.hanning(size)
    starts = range(0, max(1, len(source) - size + 1), hop)
    frames = np.stack([source[start : start + size] for start in starts]) if len(source) >= size else source[None, :size]
    magnitude = np.abs(np.fft.rfft(frames * window[None, :, None], axis=1)).mean(axis=0)
    count = math.ceil(length / hop) + 4
    phases = np.random.default_rng(6764).uniform(0.0, 2 * np.pi, (count, magnitude.shape[0]))
    out = np.zeros((count * hop + size, source.shape[1]))
    for index in range(count):
        spectrum = magnitude * np.exp(1j * phases[index])[:, None]
        out[index * hop : index * hop + size] += np.fft.irfft(spectrum, n=size, axis=0) * window[:, None]
    held = out[size : size + length]
    wanted = float(np.sqrt(np.mean(source**2)))
    made = float(np.sqrt(np.mean(held**2))) if length else 0.0
    return held * (wanted / made) if made > 0 else held


def padded_samples(take: Any, rate: int, seconds: float) -> tuple[Any, float]:
    """*take* (``[frames, channels]``) held to *seconds*: ``(the samples, the second its hold starts at)``.

    The take is kept sample for sample up to the end of its last :data:`PAD_HOLD_SECONDS` still within
    :data:`PAD_ENDING_DB` of its level (the median of its sounding 50 ms windows): the start of its own ending. That
    second's sound is held (:func:`_frozen`), crossfaded in over the take's ending across
    :data:`PAD_CROSSFADE_SECONDS`, equal-power, and faded out over the last :data:`PAD_FADE_SECONDS`, ending at
    silence on *seconds*. :class:`MusicError` for a silent take, or one with no second at its level."""
    import numpy as np

    total = round(seconds * rate)
    if total <= len(take):
        return take[:total], len(take) / rate
    step = max(1, round(0.05 * rate))
    count = len(take) // step
    levels = np.sqrt(np.mean(take[: count * step].reshape(count, step, take.shape[1]) ** 2, axis=(1, 2)))
    sounding = levels[levels > 1e-4]
    if not len(sounding):
        raise MusicError("The take is silent: nothing to hold. Refit without pad composes the track again.")
    floor = float(np.median(sounding)) * 10 ** (-PAD_ENDING_DB / 20)
    held_until = (int(np.nonzero(levels >= floor)[0][-1]) + 1) * step
    hold, cross = round(PAD_HOLD_SECONDS * rate), round(PAD_CROSSFADE_SECONDS * rate)
    # A take that stops at its level has no ending to cross over: the hold starts a crossfade before its end.
    held_until = min(held_until, max(hold, len(take) - cross))
    if held_until < hold:
        raise MusicError("The take has no second at its level to hold. Refit without pad composes the track again.")
    tail = np.zeros((total - held_until, take.shape[1]))
    ending = take[held_until : held_until + len(tail)]
    pad = _frozen(take[held_until - hold : held_until].astype(np.float64), len(tail))
    cross = min(cross, len(tail))
    phase = (np.arange(cross) + 0.5) / max(cross, 1) * (np.pi / 2)
    tail[: len(ending)] = ending
    tail[:cross] = tail[:cross] * np.cos(phase)[:, None] + pad[:cross] * np.sin(phase)[:, None]
    tail[cross:] = pad[cross:]
    fade = min(round(PAD_FADE_SECONDS * rate), len(tail) - cross)
    if fade > 0:
        tail[len(tail) - fade :] *= np.cos((np.arange(fade) + 0.5) / fade * (np.pi / 2))[:, None]
    peak = float(np.max(np.abs(tail))) if len(tail) else 0.0
    if peak > 0.99:
        tail *= 0.99 / peak
    return np.concatenate([take[:held_until], tail.astype(np.float32)]), held_until / rate


async def pad_take(take_path: str, *, seconds: float, workdir: Path) -> tuple[str, str]:
    """The take at *take_path* held to *seconds* with no model call (:func:`padded_samples`): ``(the padded
    take's path - a 32-bit float WAV at the take's own rate and channels -, what its notes say)``."""
    from app.services.video_studio import encoder

    info = await encoder.probe(take_path)
    if not info.has_audio or not info.sample_rate or not info.channels:
        raise MusicError("The take's audio could not be read: refit without pad composes the track again.")
    rate, channels = info.sample_rate, info.channels
    take = await _decoded(take_path, workdir, "take", rate=rate, channels=channels)
    padded, held_at = await asyncio.to_thread(padded_samples, take, rate, seconds)
    out_path = str(workdir / "padded.wav")
    await _written(padded, out_path, workdir, rate=rate, codec="pcm_f32le")
    note = (
        f"Padded with no model call: the take kept as it was up to {held_at:.1f} s, its chord there held "
        f"{seconds - held_at:.1f} s and faded out over its last {min(PAD_FADE_SECONDS, seconds - held_at):.1f} s, "
        f"to {seconds:.0f} s."
    )
    return out_path, note


def serialize_music(row: Any) -> dict:
    params = dict(row.params or {})
    return {
        "id": str(row.id),
        "user_id": str(row.user_id) if row.user_id else None,
        "name": row.name,
        "provider": row.provider,
        "model": row.model,
        "prompt": row.prompt,
        "params": params,
        "status": row.status,
        "progress": row.progress,
        "error": row.error,
        "duration_ms": row.duration_ms,
        "file_id": str(row.file_id) if row.file_id else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
    }


async def ready_file(db: Any, music_id: str) -> object:
    """The file of an AI track a video plays, or the reason it cannot play yet (``RuntimeError``)."""
    from app.models.video_studio import JOB_CANCELLED, JOB_FAILED, JOB_READY, VideoMusic

    row = await db.get(VideoMusic, uuid.UUID(str(music_id)))
    label = (row.name if row is not None else None) or music_id
    if row is not None and row.status in (JOB_FAILED, JOB_CANCELLED):
        raise RuntimeError(
            f"The AI track {label} {row.status} ({row.error or 'no reason given'}): generate it again, or choose "
            "another music."
        )
    if row is None:
        raise RuntimeError(f"The AI track {label} no longer exists: choose another music.")
    if row.status != JOB_READY or not row.file_id:
        raise RuntimeError(f"The AI track {label} is still being composed: render once it is ready.")
    return row.file_id


# ── Saving a video that asks for an AI track ──────────────────────────────


async def start_for_spec(
    db: Any,
    user: Any,
    authored: dict,
    *,
    planned_seconds: float,
    previous: dict | None = None,
    conversation_id: object = None,
) -> tuple[int, list[str]]:
    """Start the AI track a spec's ``music.prompt`` asks for, writing its id as ``music_id`` in *authored*.

    A draft saved without the id (an editor's) keeps the one *previous* has
    for the same prompt; a prompt that changed gets a new track. Returns how
    many started and a note when none could (no music model): the video
    saves and renders without music then, never failing on it.
    """
    from app.models.video_studio import VideoMusic

    music = authored.get("music")
    if not isinstance(music, dict) or music.get("enabled") is False:
        return 0, []
    prompt = (music.get("prompt") or "").strip()
    if not prompt or music.get("track") or music.get("file_id"):
        return 0, []
    old = (previous or {}).get("music") if isinstance((previous or {}).get("music"), dict) else {}
    if not music.get("music_id") and old.get("music_id") and (old.get("prompt") or "").strip() == prompt:
        music["music_id"] = old["music_id"]
    if music.get("music_id"):
        row = await db.get(VideoMusic, uuid.UUID(str(music["music_id"])))
        if row is not None and (row.prompt or "").strip() == prompt[:2000]:
            return 0, []
    try:
        row, _label = await start_music(
            db,
            user,
            prompt=prompt,
            duration_seconds=fitted_seconds(planned_seconds),
            model=music.get("model"),
            provider_id=music.get("provider_id"),
            instrumental=music.get("instrumental", True),
            conversation_id=conversation_id,
            name=f"Music for {str(authored.get('title') or 'a video')[:150]}",
        )
    except MusicError as exc:
        music.pop("music_id", None)
        return 0, [f"No AI track was started for the music: {exc} Choose a built-in track or upload one."]
    music["music_id"] = str(row.id)
    return 1, []


# ── Beds ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Bed:
    """What decides a bed: its source, the video's length and how the track is fitted to it."""

    kind: str  # track | music | file
    ref: str
    duration: float
    start: float = 0.0
    loop: bool = True
    ending: str = "fade"
    fade_in: float = 1.0
    fade_out: float = 2.5
    #: The scenes a built-in score plays under: ``[(start, kind, narrated)]``.
    cues: tuple[tuple[float, str, bool], ...] = field(default_factory=tuple)

    @classmethod
    def of(cls, music: Any, duration: float, cues: list | None = None) -> Bed | None:
        """The bed of a spec's ``Music`` under a video of *duration*, or ``None`` when nothing plays yet."""
        source = music.source if music is not None else None
        if source is None:
            return None
        kind, ref = source
        return cls(
            kind=kind,
            ref=ref,
            duration=round(float(duration), 2),
            start=round(float(music.start), 2) if kind != "track" else 0.0,
            loop=bool(music.loop),
            ending=music.ending,
            fade_in=round(float(music.fade_in), 2),
            fade_out=round(float(music.fade_out), 2),
            cues=tuple((round(float(c.start), 2), c.kind, bool(c.narrated)) for c in (cues or []))
            if kind == "track"
            else (),
        )

    @classmethod
    def from_claims(cls, data: dict) -> Bed:
        return cls(
            kind=str(data["kind"]),
            ref=str(data["ref"]),
            duration=float(data["duration"]),
            start=float(data.get("start", 0.0)),
            loop=bool(data.get("loop", True)),
            ending=str(data.get("ending", "fade")),
            fade_in=float(data.get("fade_in", 1.0)),
            fade_out=float(data.get("fade_out", 2.5)),
            cues=tuple((float(c[0]), str(c[1]), bool(c[2])) for c in data.get("cues") or ()),
        )

    def claims(self) -> dict:
        data = asdict(self)
        data["cues"] = [list(c) for c in self.cues]
        return data

    def digest(self) -> str:
        raw = json.dumps({"v": BED_VERSION, **self.claims()}, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def score_cues(self):
        from app.services.video_studio.score import Cue

        return [Cue(start, kind, narrated) for start, kind, narrated in self.cues]


def audition_bed(track_id: str) -> Bed:
    """A built-in track heard on its own: half a minute with its arc (open, lift, peak, end)."""
    return Bed(kind="track", ref=track_id, duration=AUDITION_SECONDS, fade_in=0.5, fade_out=1.5)


def silent_tail_words(playable: float, video_seconds: float) -> str:
    """``it fades out at 174 s, leaving the video's last 1 s without music``: a track asked to play once that ends
    before the video - never looped."""
    return (
        f"it fades out at {max(playable, 0.0):.0f} s, leaving the video's last "
        f"{max(video_seconds - playable, 0.0):.0f} s without music"
    )


def bed_length_note(bed: Bed, source_seconds: float) -> str | None:
    """The track's length against the video's, as a render's notes say it; ``None`` for a built-in score."""
    if bed.kind == "track" or source_seconds <= 0:
        return None
    what = "AI track" if bed.kind == "music" else "music file"
    playable = max(source_seconds - bed.start, 0.0)
    head = f"Music: the {what} lasts {source_seconds:.0f} s"
    if bed.start:
        head += f" ({playable:.0f} s from its {bed.start:g} s start)"
    head += f" under the {bed.duration:.0f} s video"
    if playable >= bed.duration - 0.05:
        ending = "its own end lands on the video's end" if bed.ending == "natural" else "it fades out at the video's end"
        return f"{head}: {ending}."
    if bed.loop:
        return f"{head}: it loops at {playable:.0f} s."
    return f"{head}: asked to play once, {silent_tail_words(playable, bed.duration)}."


def played_record(bed: Bed | None, file_id: object | None) -> dict:
    """What a render records of the music it played (``VideoRender.music``).

    The source (``track`` / ``music`` / ``file`` and its id) and, for an AI
    track, the file it played: a refit composes the same track again into
    another file, the video keeping its ``music_id`` and its revision.
    ``{"kind": "none"}`` for a render that played none.
    """
    if bed is None:
        return {"kind": "none"}
    record: dict[str, Any] = {"kind": bed.kind, "ref": bed.ref}
    if bed.kind == "music" and file_id:
        record["file_id"] = str(file_id)
    return record


_PLAYED_WORDS = {"track": "the built-in track `{}`", "music": "the AI track `{}`", "file": "an uploaded sound"}


def _played_words(kind: str, ref: str) -> str:
    return _PLAYED_WORDS[kind].format(ref) if kind in _PLAYED_WORDS else "no music"


async def render_music_note(db: Any, render: Any, music: Any, *, music_changed: bool = False) -> str | None:
    """Whether *render* was made with the music *music* (a spec's ``Music``, as it plays now) names.

    ``None`` when neither plays any. A render made before renders recorded
    their music cannot tell: it says so once, from the save that changes the
    video's music (*music_changed*), never on every save and read
    until the video is rendered again. An AI track composed again since the
    render (a refit keeps its id) is told apart by the file it played.
    """
    from app.models.video_studio import JOB_PENDING, JOB_READY, JOB_RUNNING, VideoMusic

    source = music.source if music is not None else None
    record = getattr(render, "music", None)
    if not isinstance(record, dict):
        if source is None or not music_changed:
            return None
        return (
            f"The current render `{render.id}` does not record the music it played (made before renders recorded "
            "it): render again to be sure it plays the music the video has now."
        )
    played = (str(record.get("kind") or "none"), str(record.get("ref") or ""))
    if source is None:
        if played[0] == "none":
            return None
        return (
            f"The current render `{render.id}` plays {_played_words(*played)}, which the video no longer plays: "
            "render again for the video as it is now."
        )
    if played != source:
        return (
            f"The current render `{render.id}` was made with {_played_words(*played)}, not with "
            f"{_played_words(*source)} the video plays now: its music notes describe that one; render again to "
            "hear this one."
        )
    if source[0] != "music":
        return f"The current render `{render.id}` was made with the music the video plays now."
    row = await db.get(VideoMusic, uuid.UUID(source[1])) if _is_uuid(source[1]) else None
    track = f"the AI track `{source[1]}`"
    if row is None:
        return f"The current render `{render.id}` plays {track}, which no longer exists: choose another music."
    if row.status in (JOB_PENDING, JOB_RUNNING):
        return (
            f"The current render `{render.id}` plays an earlier take of {track}, being composed again now: its music "
            f"notes describe that take. Render again once it is ready (get_video_jobs(music_ids=['{source[1]}']))."
        )
    if row.status != JOB_READY:
        return (
            f"The current render `{render.id}` plays an earlier take of {track}, whose new composition "
            f"{row.status}: generate it again (generate_music(music_id='{source[1]}')) before rendering."
        )
    if str(row.file_id or "") != str(record.get("file_id") or ""):
        return (
            f"The current render `{render.id}` was made with an earlier take of {track} (composed again since): its "
            "music notes describe that take; render again to hear the new one."
        )
    return f"The current render `{render.id}` was made with the music the video plays now."


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(str(value))
    except ValueError:
        return False
    return True


#: Uploaded music files measured by this process, by their storage object: a probe costs a download.
_FILE_SECONDS: dict[str, float] = {}
_FILE_SECONDS_KEPT = 256


async def _file_seconds(file_id: str) -> float | None:
    """How long an uploaded music file plays (``None``: gone or not audio), read once per stored object."""
    from app.services.video_studio import assets, encoder

    row = await assets.load_file(file_id)
    if row is None:
        return None
    if row.s3_object_name in _FILE_SECONDS:
        return _FILE_SECONDS[row.s3_object_name]
    with tempfile.TemporaryDirectory(prefix="xtm-music-length-") as work:
        path = str(Path(work) / "source")
        if await assets.download_file(file_id, path) is None:
            return None
        try:
            seconds = (await encoder.probe(path)).duration
        except encoder.EncoderError:
            return None
    if len(_FILE_SECONDS) >= _FILE_SECONDS_KEPT:
        _FILE_SECONDS.pop(next(iter(_FILE_SECONDS)))
    _FILE_SECONDS[row.s3_object_name] = seconds
    return seconds


async def playable_seconds(db: Any, music: Any) -> tuple[float, bool] | None:
    """How long the music's source plays from its ``start``, and whether it is still being composed at that length.

    An AI track is measured by its file once made, by the length it was asked
    for while composed; an uploaded file by its own length. ``None`` for a
    built-in score (composed for the video's length), when nothing plays yet,
    and when the length cannot be read.
    """
    from app.models.video_studio import JOB_PENDING, JOB_READY, JOB_RUNNING, VideoMusic

    source = music.source if music is not None else None
    if source is None or source[0] == "track":
        return None
    kind, ref = source
    composing = False
    if kind == "music":
        row = await db.get(VideoMusic, uuid.UUID(ref))
        if row is None:
            return None
        if row.status == JOB_READY and row.duration_ms:
            seconds = row.duration_ms / 1000
        elif row.status in (JOB_PENDING, JOB_RUNNING):
            seconds, composing = float((row.params or {}).get("duration_seconds") or 0), True
        else:
            return None
    else:
        seconds = await _file_seconds(ref) or 0.0
    if not seconds:
        return None
    return max(seconds - float(music.start or 0.0), 0.0), composing


def _seconds(value: float) -> str:
    """``146.5`` / ``144``: a length in seconds as a warning writes it, to the tenth."""
    return f"{round(float(value), 1):g}"


def outgrows(music: Any, playable: float | None, video_seconds: float) -> bool:
    """Whether a video of *video_seconds* outgrew the looping AI track *music* plays, *playable* seconds of it from its
    ``start``, by more than :data:`OUTGROWN_SECONDS`."""
    source = music.source if music is not None else None
    if source is None or source[0] != "music" or not music.loop or playable is None:
        return False
    return playable < video_seconds - OUTGROWN_SECONDS


def outgrown_words(playable: float, video_seconds: float, *, composing: bool = False) -> str:
    """What a save warns when a video outgrew its AI track (:func:`outgrows`); *composing*: the track is still
    being composed at that length."""
    being = " (being composed at that length)" if composing else ""
    padded = (
        f"; {pad_offer(playable, video_seconds)}"
        if not composing and video_seconds - playable < PAD_PROPOSED_SECONDS
        else ""
    )
    return (
        f"track {_seconds(playable)} s{being} under a {_seconds(video_seconds)} s video: refit or accept the loop - "
        "generate_music(video_id, refit: true, extend: true) keeps the take and composes its tail again to the "
        f"video's length{padded}; left as it is, the track loops at {_seconds(playable)} s"
    )


async def length_note(db: Any, music: Any, video_seconds: float) -> str | None:
    """When the music stops or loops before a video of *video_seconds* ends, as a save line says it.

    ``None`` when it lasts the whole video, for a built-in score (composed for
    the length), when nothing plays yet (:func:`playable_seconds`) and for an
    AI track the video outgrew (:func:`outgrows`): the save's quality check
    warns of that one, on the plan its narrations are spoken at.
    """
    if video_seconds <= 0:
        return None
    measured = await playable_seconds(db, music)
    if measured is None:
        return None
    playable, composing = measured
    kind = music.source[0]
    if playable >= video_seconds - 0.5 or outgrows(music, playable, video_seconds):
        return None
    being = " (being composed at that length)" if composing else ""
    remedy = (
        " generate_music(video_id, refit: true) composes it again at the video's length."
        if kind == "music"
        else " Upload a longer track, or start it earlier (music.start)."
    )
    if kind == "music" and not composing and video_seconds - playable < PAD_PROPOSED_SECONDS:
        # A few seconds short, held with no model call before any paid refit.
        remedy = f" {pad_offer(playable, video_seconds)}, or refit: true composes it again at the video's length."
    if music.loop:
        return f"Music loops at {playable:.0f} s of {video_seconds:.0f} s{being}: the track is shorter than the video.{remedy}"
    return (
        f"Music ends at {playable:.0f} s of {video_seconds:.0f} s{being} (loop: false): "
        f"{silent_tail_words(playable, video_seconds)}.{remedy}"
    )


async def make_bed(bed: Bed, out_path: str, *, source_path: str | None, workdir: Path) -> list[str]:
    """Prepare *bed* as a 48 kHz float WAV at the reference loudness (the render's input).

    Returns the track's length against the video's (:func:`bed_length_note`). A built-in score is
    composed once per bed (its digest): the next render of the video - at another quality, on another worker -
    takes it from the render cache (``render_cache``).
    """
    from app.services.video_studio import encoder, render_cache, score

    if bed.kind == "track":
        if await render_cache.get_file("beds", bed.digest(), out_path, suffix=".wav"):
            return []
        score_path = str(workdir / f"score-{bed.digest()[:12]}.wav")
        async with _PREPARING:
            await asyncio.to_thread(score.render_score, bed.ref, bed.duration, bed.score_cues() or None, score_path)
        try:
            await encoder.prepare_bed(
                score_path, out_path, duration=bed.duration, loop=False, fade_in=bed.fade_in, fade_out=bed.fade_out
            )
        finally:
            Path(score_path).unlink(missing_ok=True)
        await render_cache.put_file("beds", bed.digest(), out_path, suffix=".wav")
        return []
    if not source_path:
        raise RuntimeError("The music's file is gone.")
    source_seconds = (await encoder.probe(source_path)).duration
    note = bed_length_note(bed, source_seconds)
    await encoder.prepare_bed(
        source_path,
        out_path,
        duration=bed.duration,
        start=bed.start,
        loop=bed.loop,
        ending=bed.ending,
        fade_in=bed.fade_in,
        fade_out=bed.fade_out,
    )
    return [note] if note else []


async def source_file_id(db: Any, bed: Bed) -> object | None:
    """The file a bed is made from (``None`` for a built-in track); ``RuntimeError`` when it cannot play."""
    if bed.kind == "track":
        return None
    if bed.kind == "music":
        return await ready_file(db, bed.ref)
    return uuid.UUID(bed.ref)


def _cached(digest: str) -> Path:
    return _CACHE_DIR / f"{digest}.m4a"


def _trim_cache() -> None:
    try:
        entries = sorted(
            ((p.stat().st_mtime, p.stat().st_size, p) for p in _CACHE_DIR.glob("*.m4a")), key=lambda e: e[0]
        )
    except OSError:
        return
    total = sum(size for _mtime, size, _p in entries)
    for _mtime, size, path in entries:
        if total <= _CACHE_BYTES:
            break
        path.unlink(missing_ok=True)
        total -= size


async def preview_bed(bed: Bed, file_id: object | None) -> Path:
    """The bed as the editor and the library play it (AAC), prepared once per digest and kept on disk."""
    from app.services.video_studio import assets, encoder

    digest = bed.digest()
    target = _cached(digest)
    if target.exists():
        os.utime(target, (time.time(), time.time()))
        return target
    lock = _LOCKS.setdefault(digest, asyncio.Lock())
    async with lock:
        if target.exists():
            return target
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        workdir = Path(tempfile.mkdtemp(prefix="xtm-bed-"))
        try:
            source = None
            if file_id is not None:
                source = str(workdir / "source")
                if await assets.download_file(file_id, source) is None:
                    raise RuntimeError("The music's file is gone.")
            wav = str(workdir / "bed.wav")
            await make_bed(bed, wav, source_path=source, workdir=workdir)
            partial = workdir / "bed.m4a"
            await encoder.encode_preview_bed(wav, str(partial))
            shutil.move(str(partial), target)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
            _LOCKS.pop(digest, None)
    _trim_cache()
    return target
