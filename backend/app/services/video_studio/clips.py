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

"""AI-generated clips: any video model the platform's providers offer.

A clip is made by a provider's ``generate_video`` (``app.llm.video``): the
OpenAI Videos API on every OpenAI-shaped provider (OpenAI and Azure OpenAI
Sora, and whatever an OpenAI-compatible gateway serves behind it - LiteLLM in
front of Veo on Vertex or Runway, a custom endpoint, the Filigran gateway),
Veo on Gemini. Which provider and model: what the agent named (one of the
models ``video_studio_capabilities`` lists), else the platform's choice
(Settings > AI and models > Media > Video generation), else the first provider with a
built-in video model - ``router.get_video_provider``, which also refuses a
provider that excludes the person.

The request is asked for, polled and downloaded inside the ``video_clip``
orchestration job, so a clip that takes minutes never holds a chat turn; the
provider snaps a duration or a size to what the model makes and says so.

A shot may start from a frame: an image as it is, or a frame of a
video - a recording, another AI shot, a video file - which every video model
takes as a start image (:func:`resolve_reference`, :func:`start_frame`). A
frame cut from a video is kept as a file of the shot's conversation,
so the person sees what the shot opened on and a shot generated again starts
from that very file; it has the shot's shape, or is cropped to it; and it can
be looked at before paying (:func:`preview_start_frame`), a recording's page
still loading flagged with the moment it settles (:func:`settle`).

A shot is made for a video: it records the art direction of the
template that video wears (:func:`resolve_direction` - the template named,
the video's, else the platform's default; none in free style) and the worker
sends the video model the prompt that direction shapes
(``templates.direct_shot``, through :func:`build_request`): the template's
look added to a shot drawn from text, what it avoids added to the negative
prompt, which a model without one reads in the prompt.
"""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.core.utils import format_exception_message
from app.llm.video import (
    VideoGenerationError,
    VideoJobState,
    VideoModelNotServedError,
    VideoRequest,
    VideoRequestRefusedError,
    VideoResult,
)

logger = logging.getLogger(__name__)


class ClipError(RuntimeError):
    """A clip the provider could not make; ``str()`` is safe to show.

    ``about_model``: the reason is the video model or its provider (none
    available, retired, unknown) - another listed model is then the remedy.
    ``not_served``: the provider answered HTTP 404 for the model
    (``video_served.sentence``: ``B is not served by Kling (HTTP 404 ...)``).
    """

    def __init__(
        self, message: str, *, about_model: bool = False, not_served: str | None = None, unpaid: bool = False
    ) -> None:
        super().__init__(message)
        self.about_model = about_model or not_served is not None
        self.not_served = not_served
        #: Refused on its arguments before any job existed upstream: nothing made or paid, so no try of
        #: its prompt and start frame (:func:`uncount_regeneration`).
        self.unpaid = unpaid or not_served is not None


class StartPictureError(ClipError):
    """A start picture no provider can read: refused when the shot is asked for, before anything is paid."""


async def resolve(
    db: Any,
    user_id: object,
    *,
    model: str | None = None,
    provider_id: object = None,
    prefer: Sequence[tuple[object, str]] = (),
    notes: list[str] | None = None,
):
    """``(provider, model, config)`` a clip is made with, or :class:`ClipError`. A model several providers offer is
    asked of the first of *prefer* ``(provider id, why)`` offering it, else of the platform's provider
    (``router.get_video_provider``): which one and why is added to *notes*."""
    from app.llm.router import VideoModelUnusableError, get_video_provider

    try:
        return await get_video_provider(
            db, user_id=user_id, provider_id=provider_id, model=(model or "").strip() or None, prefer=prefer, chose=notes
        )
    except ValueError as exc:
        unserved = exc.reason if isinstance(exc, VideoModelUnusableError) and exc.not_served else None
        raise ClipError(str(exc), about_model=True, not_served=unserved) from None


#: Where the models an AI shot may be made on are set: what a refused model names.
VIDEO_MODELS_SETTING = (
    "Settings > AI and models > Media > Video generation (the default video model and the video fallback models)"
)


async def configured_models(db: Any) -> tuple[str | None, list[str]]:
    """``(default, fallbacks)`` the platform configures for AI shots (``router.configured_video_models``)."""
    from app.llm.router import configured_video_models

    return await configured_video_models(db)


def configured_model(model: str | None, *, default: str | None, fallbacks: Sequence[str]) -> str | None:
    """The model a call may make a shot on, or :class:`ClipError` naming the configuration.

    With a configured default video model, a shot is made on it - ``None``,
    the platform's choice (``router.get_video_provider`` with no model: the
    default on its configured provider) - when the call names no model or the
    default, or on one of the configured video fallback models it names; any
    other model is refused before anything is queued or paid. Without a
    configured default the model named is kept: there is no model to hold it
    to.
    """
    name = (model or "").strip() or None
    if default is None:
        return name
    if name is None or name == default:
        return None
    if name in fallbacks:
        return name
    allowed = ", ".join([default, *fallbacks])
    raise ClipError(
        f"model {name} is not allowed by configuration: AI shots are made on the configured default video model "
        f"{default}" + (f" or a configured video fallback ({allowed})" if fallbacks else " (no video fallback is configured)")
        + f". Leave model out, or change {VIDEO_MODELS_SETTING}",
        about_model=True,
    )


#: Why a model several providers offer is asked of the one a video's shots of it were made on (:func:`resolve`).
VIDEO_SHOTS_CHOICE = "the provider this video's shots of it are made on"
#: ... of the one the shot generated again was made on.
OWN_SHOT_CHOICE = "the provider this shot was made on"


async def video_providers(db: Any, user: Any, video_id: object, model: str | None) -> list[tuple[str, str]]:
    """``[(provider id, why)]`` a shot of *model* asked for the video *video_id* prefers when several providers offer
    that model (:func:`resolve`): the providers the video's own shots of it were made on, the latest first - the
    video's default for that model. Empty without a video, a model, or a shot of it the video plays."""
    from sqlalchemy import select

    from app.core.utils import safe_uuid
    from app.models.video_studio import VideoClip
    from app.services.video_studio.projects import load_visible_project
    from app.services.video_studio.spec import referenced_ids

    model = (model or "").strip()
    if not video_id or not model:
        return []
    project = await load_visible_project(db, video_id, user)
    ids = [uid for raw in referenced_ids(project.spec or {})["clip"] if (uid := safe_uuid(raw)) is not None] if project else []
    if not ids:
        return []
    rows = (
        (await db.execute(select(VideoClip).where(VideoClip.id.in_(ids), VideoClip.model == model).order_by(VideoClip.created_at.desc())))
        .scalars()
        .all()
    )
    found = dict.fromkeys(str(pid) for row in rows if (pid := (row.params or {}).get("provider_id")))
    return [(pid, VIDEO_SHOTS_CHOICE) for pid in found]


#: How long the provider is given to say an account's credits before a shot is asked: unanswered, unknown.
CREDITS_READ_SECONDS = 10.0


async def credits_for(provider: Any, request: VideoRequest) -> Any:
    """What *provider* says of the account's credits for *request* (``BaseLLMProvider.video_credits``): a
    ``video.VideoCredits``, or ``None`` when it does not say - a read that fails or takes too long included."""
    import asyncio

    from app.llm.video import VideoCredits

    reader = getattr(provider, "video_credits", None)
    if reader is None:
        return None
    try:
        credits = await asyncio.wait_for(reader(request), CREDITS_READ_SECONDS)
    except Exception as exc:  # a provider that cannot say is asked as before: its own answer then decides
        logger.info("Video credits of %s not read: %s", request.model, format_exception_message(exc))
        return None
    return credits if isinstance(credits, VideoCredits) else None


async def refuse_unpaid(provider: Any, config: Any, request: VideoRequest) -> None:
    """Refuse, before anything is queued or paid, a shot the account's credits cannot pay for: ``quota: N
    remaining, M needed`` - the cost the provider names, else the one its last quota refusal of the model named
    (``provider_cooling``). Never another model offered: the shot is the author's on this one."""
    from app.llm.video import VideoCredits
    from app.services.video_studio import provider_cooling

    credits = await credits_for(provider, request)
    if credits is None or credits.remaining is None:
        return
    if credits.needed is None:
        cooling = await provider_cooling.cooling_of(getattr(config, "id", None), model=request.model)
        if cooling is None or cooling.reason != provider_cooling.QUOTA or cooling.needed is None:
            return
        credits = VideoCredits(remaining=credits.remaining, needed=cooling.needed)
    if credits.short():
        label = getattr(config, "display_name", None) or getattr(config, "provider", None) or "its provider"
        raise ClipError(
            f"{credits.words()} - the {label} account cannot pay for this shot of {request.model}: nothing was "
            "queued or paid; ask it again once the account has the credits"
        )


#: The longest prompt a shot is asked with.
MAX_PROMPT_CHARS = 4000
#: The params keys of the art direction a shot was made under, and of a shot asked in free style.
DIRECTION = "direction"
FREESTYLE = "freestyle"


def build_request(
    *,
    model: str,
    prompt: str,
    aspect_ratio: str = "16:9",
    duration_seconds: int = 8,
    resolution: str = "1080p",
    negative_prompt: str | None = None,
    sound: bool | None = None,
) -> VideoRequest:
    return VideoRequest(
        model=model,
        prompt=prompt.strip()[:MAX_PROMPT_CHARS],
        aspect_ratio=aspect_ratio,
        duration_seconds=max(1, min(int(duration_seconds or 8), 20)),
        resolution=resolution,
        negative_prompt=negative_prompt or None,
        sound=sound,
    )


def directed(params: dict) -> dict | None:
    """The art direction a shot applies: ``None`` without one, or with no look and nothing avoided."""
    direction = params.get(DIRECTION)
    return direction if isinstance(direction, dict) and (direction.get("style") or direction.get("avoid")) else None


#: The params key of a shot asked silent because the video it was made for drops the clips' own sound.
SILENT_FOR_VIDEO = "silent_for_video"


async def video_drops_clip_sound(db: Any, user: Any, video_id: object) -> bool:
    """Whether the video *user* makes a shot for drops every clip's own sound (``clip_sound: off``, its template applied)."""
    from app.services.video_studio.projects import effective_spec, load_visible_project

    project = await load_visible_project(db, video_id, user) if video_id else None
    if project is None:
        return False
    try:
        spec = (await effective_spec(db, project, user)).spec
    except Exception:  # a video that no longer plans: what its author wrote
        spec = project.spec or {}
    return (spec or {}).get("clip_sound") == "off"


def sound_note(params: dict) -> str | None:
    """Why a shot was asked silent when its call did not say, as its notes say it; ``None`` otherwise."""
    if params.get(SILENT_FOR_VIDEO):
        return "Asked silent: the video it was made for drops the clips' own sound (clip_sound off); sound: true keeps it."
    return None


def starts_from_picture(params: dict) -> bool:
    return bool(params.get("reference_file_id") or params.get("reference_video"))


def takes_negative_prompt(params: dict, provider: Any, model: str) -> bool:
    """Whether the art direction's avoided terms reach *model*: it has a direction to apply and a negative prompt field.

    The provider is asked only when there is something to apply.
    """
    return directed(params) is not None and provider is not None and provider.takes_negative_prompt(model)


def shot_prompts(params: dict, prompt: str, negative_prompt: str | None, *, negative_field: bool) -> tuple[str, str | None]:
    """The prompt and negative prompt a shot is sent with: its art direction applied (``templates.direct_shot``)."""
    from app.services.video_studio.templates import direct_shot

    direction = directed(params)
    if direction is None:
        return prompt, negative_prompt
    return direct_shot(
        prompt,
        negative_prompt,
        direction,
        start_frame=starts_from_picture(params),
        negative_field=negative_field,
        limit=MAX_PROMPT_CHARS,
    )


async def resolve_direction(
    db: Any, user: Any, *, video_id: object = None, template_id: object = None, freestyle: bool = False
) -> dict | None:
    """The art direction a shot of *user* is made under (``templates.shot_direction``), or :class:`ClipError`.

    ``freestyle``: none. ``template_id``: a template the person may use (a
    shot asked before its video exists). ``video_id``: the template a video
    the person can open wears (none for a free-style video). Neither - or a
    video whose template the person can no longer use - the platform's
    default video template, the one a video that names none wears.
    """
    from app.services.video_studio import templates

    if freestyle:
        return None
    if template_id:
        row = await templates.load_usable_template(db, template_id, user)
        if row is None:
            raise ClipError(f"No template `{template_id}` you may use (video_studio_capabilities lists the video templates).")
        return templates.shot_direction(templates.VideoTemplate.from_container(row))
    if video_id:
        from app.services.video_studio.projects import load_visible_project

        project = await load_visible_project(db, video_id, user)
        if project is None:
            raise ClipError(f"No video `{video_id}` you can open to make the shot for.")
        if (project.spec or {}).get("freestyle") or project.template_container_id is None:
            return None
        row = await templates.load_usable_template(db, project.template_container_id, user)
        if row is not None:
            return templates.shot_direction(templates.VideoTemplate.from_container(row))
    row = await templates.default_template(db)
    return templates.shot_direction(templates.VideoTemplate.from_container(row)) if row is not None else None


async def _kept_direction(db: Any, user: Any, params: dict) -> dict | None:
    """The direction a shot generated again is made under, when the call names no other.

    The template it was made for, read again (a brand update reaches the
    new take); its last snapshot when the person can no longer use that
    template; none for a shot asked in free style; the default for a shot
    made before shots had one.
    """
    from app.services.video_studio import templates

    if params.get(FREESTYLE):
        return None
    kept = params.get(DIRECTION)
    if not isinstance(kept, dict):
        return await resolve_direction(db, user)
    row = await templates.load_usable_template(db, kept.get("template_id"), user)
    return templates.shot_direction(templates.VideoTemplate.from_container(row)) if row is not None else kept


def _record_direction(params: dict, direction: dict | None) -> None:
    params.pop(DIRECTION, None)
    params.pop(FREESTYLE, None)
    if direction is None:
        params[FREESTYLE] = True
    else:
        params[DIRECTION] = direction


def direction_note(params: dict, *, negative_field: bool) -> str | None:
    """The line a shot's notes carry about the art direction it was sent with (what of it reached the model)."""
    direction = directed(params)
    if direction is None:
        return None
    applied = [
        *(["its look"] if direction.get("style") and not starts_from_picture(params) else []),
        *(["what it avoids"] if direction.get("avoid") and negative_field else []),
    ]
    if not applied:
        return None
    return f"Under the {direction.get('template') or 'template'} art direction ({' and '.join(applied)})."


#: No shot is made larger than 1080p, so the frame a video gives is no larger either.
START_FRAME_MAX_SIDE = 1920
#: A moment this close to a video's end is its last frame (a seek there can land past it).
_END_MARGIN_SECONDS = 0.05
#: The picture types every video provider takes as a start frame as they are.
START_PICTURE_TYPES = frozenset({"image/png", "image/jpeg"})
#: The largest picture file a shot starts from.
MAX_START_PICTURE_BYTES = 40 * 1024 * 1024


def _looks_like_svg(data: bytes) -> bool:
    head = data[:4096].decode("utf-8", errors="ignore").lstrip("\ufeff").lstrip().lower()
    return head.startswith("<svg") or (head.startswith(("<?xml", "<!doctype", "<!--")) and "<svg" in head)


def _raster_start_picture(data: bytes, media_type: str) -> tuple[bytes, str]:
    """A raster picture as PNG or JPEG of at most :data:`START_FRAME_MAX_SIDE`, fully decoded first."""
    import io

    from PIL import Image

    with Image.open(io.BytesIO(data)) as image:
        image.seek(0)
        image.load()
        if media_type in START_PICTURE_TYPES and image.format in ("PNG", "JPEG") and max(image.size) <= START_FRAME_MAX_SIDE:
            return data, media_type
        mode = "RGBA" if "A" in image.getbands() or "transparency" in image.info else "RGB"
        picture = image.convert(mode)
    picture.thumbnail((START_FRAME_MAX_SIDE, START_FRAME_MAX_SIDE))
    out = io.BytesIO()
    picture.save(out, format="PNG")
    return out.getvalue(), "image/png"


async def start_picture(data: bytes, mime: str | None) -> StartFrame:
    """*data* as a start frame every provider reads, or :class:`StartPictureError`.

    A PNG or a JPEG goes as it is (made no larger than :data:`START_FRAME_MAX_SIDE`);
    an SVG - a template's logo, badge or diagram - is drawn by the platform's
    bounded rasteriser (``svg_raster``); any other picture (WebP, GIF, TIFF...)
    is re-encoded as PNG. Bytes that are no picture at all are refused.
    """
    import asyncio

    from PIL import Image, UnidentifiedImageError

    from app.core.config import settings
    from app.services.svg_raster import rasterize_svg_async

    if not data:
        raise StartPictureError("The start picture is empty.")
    media_type = (mime or "").split(";", 1)[0].strip().lower()
    if media_type == "image/svg+xml" or _looks_like_svg(data):
        png = await rasterize_svg_async(data, box=START_FRAME_MAX_SIDE, strict=settings.svg_strict_rendering)
        if not png:
            raise StartPictureError(
                "The SVG start picture could not be drawn (malformed, too complex, or referring outside itself)."
            )
        return StartFrame(png, "image/png", svg=True)
    try:
        converted, converted_type = await asyncio.to_thread(_raster_start_picture, data, media_type)
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError, SyntaxError) as exc:
        raise StartPictureError(f"The start picture could not be read as an image ({type(exc).__name__}).") from None
    return StartFrame(converted, converted_type)


async def check_start_picture(row: Any) -> StartFrame | None:
    """Refuse, before anything is paid, a picture file no provider could start a shot from; else the picture as sent."""
    from app.services.file_storage import FileNotFoundInStorageError, file_storage

    if (row.size or 0) > MAX_START_PICTURE_BYTES:
        raise StartPictureError(
            f"The picture `{row.filename}` is too large to start a shot from "
            f"({(row.size or 0) // (1024 * 1024)} MB; at most {MAX_START_PICTURE_BYTES // (1024 * 1024)} MB)."
        )
    try:
        data = await file_storage.download(row.s3_object_name)
    except FileNotFoundInStorageError:
        raise StartPictureError(f"The picture `{row.filename}` is gone from storage.") from None
    try:
        return await start_picture(data, row.content_type)
    except StartPictureError as exc:
        raise StartPictureError(f"`{row.filename}`: {exc}") from None


#: How far a frame's width / height may be from the shot's and still be its shape:
#: room for an encoder's rounding (1920x1088), not for 16:10 into 16:9.
ASPECT_TOLERANCE = 0.02
#: How a frame of another shape is fitted to the shot: its middle, cut to the shot's ratio.
CROP_CENTER = "center"


def _shape(aspect_ratio: Any) -> tuple[int, int] | None:
    """``"16:9"`` as ``(16, 9)``; ``None`` for anything that is not two positive whole numbers."""
    match = re.fullmatch(r"\s*(\d{1,3})\s*:\s*(\d{1,3})\s*", str(aspect_ratio or ""))
    if not match or not int(match[1]) or not int(match[2]):
        return None
    return int(match[1]), int(match[2])


def fits(size: tuple[int, int], shape: tuple[int, int]) -> bool:
    """Whether a frame of *size* pixels has the shot's *shape* (within :data:`ASPECT_TOLERANCE`)."""
    width, height = size
    return abs((width / height) / (shape[0] / shape[1]) - 1) <= ASPECT_TOLERANCE


def cropped_size(size: tuple[int, int], shape: tuple[int, int]) -> tuple[int, int]:
    """The middle of a *size* frame cut to *shape*, as ffmpeg's crop makes it (even sides)."""
    width, height = size
    return int(min(width, height * shape[0] / shape[1])) & ~1, int(min(height, width * shape[1] / shape[0])) & ~1


def _crop(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if value != CROP_CENTER:
        raise ClipError(f'crop is "{CROP_CENTER}" (the middle of the frame, cut to the shot\'s ratio) or unset.')
    return CROP_CENTER


def _misshapen(what: str, size: tuple[int, int], aspect_ratio: str, shape: tuple[int, int]) -> ClipError:
    """The refusal of a frame that is not the shot's shape: what it is, and how to fit it."""
    width, height = size
    middle = "x".join(str(side) for side in cropped_size(size, shape))
    other = f"{shape[1]}:{shape[0]}"
    flip = f", or make the shot {other}" if shape[0] != shape[1] and fits(size, (shape[1], shape[0])) else ""
    return ClipError(
        f'{what} is {width}x{height}, not {aspect_ratio} like the shot: pass crop: "{CROP_CENTER}" to start '
        f"from its middle {middle}{flip}."
    )


def _number(value: Any, name: str, *, whole: bool = False) -> float | None:
    """*value* as the number it was meant as, ``None`` when unset, or :class:`ClipError`."""
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ClipError(f"{name} must be a number.") from None
    if not math.isfinite(number) or number < 0:
        raise ClipError(f"{name} must be 0 or more.")
    if whole and number != int(number):
        raise ClipError(f"{name} must be a whole number.")
    return number


def _usable(row: Any, what: str) -> None:
    """Refuse a recording or an AI shot that has no video to start a shot from."""
    from app.models.video_studio import JOB_PENDING, JOB_READY, JOB_RUNNING

    if row.status in (JOB_PENDING, JOB_RUNNING):
        raise ClipError(f"{what} is still being made: start the shot from it once it is ready.")
    if row.status != JOB_READY:
        raise ClipError(f"{what} {row.status} and made no video to start a shot from.")
    if row.expired_at is not None or not row.file_id:
        raise ClipError(f"{what} has no media any more (the retention removed it): make it again first.")


def _video_source(
    kind: str, row_id: Any, file_id: Any, name: str, at: float | None, step: int | None, crop: str | None
) -> dict:
    source = {
        "kind": kind,
        "id": str(row_id),
        "file_id": str(file_id),
        "name": (name or "")[:120],
        "at_seconds": None if at is None else round(at, 3),
    }
    if step is not None:
        source["step"] = step
    if crop:
        source["crop"] = crop
    return source


def _clamped(source: dict, asked: Any, duration: float) -> dict:
    """*source* taken at its last frame, the moment asked past its end said with it."""
    return {**source, "at_seconds": None, "clamped_from": round(float(asked), 3), "lasts": round(duration, 3)}


def _check_shape(what: str, size: tuple[Any, Any], aspect_ratio: str | None, crop: str | None) -> None:
    """Refuse a frame of *size* that is not the shot's shape, unless it is cropped to it; no shot or size, no check."""
    shape = _shape(aspect_ratio)
    if crop or shape is None or not all(size):
        return
    if not fits((int(size[0]), int(size[1])), shape):
        raise _misshapen(what, (int(size[0]), int(size[1])), aspect_ratio, shape)


async def _full_id(db: Any, user: Any, model: Any, value: Any, *, noun: str, label: Any) -> Any:
    """*value* with an id prefix (8+ hexadecimal characters) of one *model* row of *user* taken for its full id,
    as ``get_video_jobs`` takes it (``jobs.resolve_job_ids``: among their own); :class:`ClipError` naming the
    candidates when it starts several. A full id, anything else, or a prefix starting none comes back unchanged
    and the caller answers its usual not found; whoever may read the row is still checked by the caller."""
    from app.services.builtin_tools.id_prefix import id_prefix, resolve_id_prefix

    if id_prefix(value) is None:
        return value
    full, ambiguous = await resolve_id_prefix(db, model, value, [model.user_id == user.id], noun=noun, label=label)
    if ambiguous:
        raise ClipError(ambiguous.removeprefix("Error: "))
    return full


async def resolve_reference(
    db: Any,
    user: Any,
    *,
    file_id: Any = None,
    recording_id: Any = None,
    clip_id: Any = None,
    at_seconds: Any = None,
    at_step: Any = None,
    aspect_ratio: str | None = None,
    crop: Any = None,
    clamp: bool = False,
) -> dict[str, Any]:
    """The params a shot's first frame is recorded as (``{}``: none), or :class:`ClipError`.

    An image is the first frame as it is (``reference_file_id``). A video - a
    recording, another AI shot, a video file - gives one of its frames
    (``reference_video``): after a recording's step the one its contact sheet
    shows, else the one at *at_seconds*, else its last, so a shot placed
    after that footage continues it. Everything is checked here, before
    anything is paid for: one source the person can read, ready, a frame
    inside it, and of the shot's *aspect_ratio* unless *crop* is
    ``"center"``, which cuts its middle to that ratio (no *aspect_ratio*: a
    frame kept as a picture, of any shape). A video file's size is
    known once it is read: the worker checks it before the provider is asked
    (:func:`start_frame`). An image is sent as it is - an animated still is
    framed by its scene - so it is neither checked nor cropped. *clamp*: a
    moment past the video's end is its last frame, the moment asked kept as
    ``clamped_from`` (a frame kept as a picture), never refused.
    """
    from app.core.utils import safe_uuid
    from app.models.file_attachment import FileAttachment
    from app.models.video_studio import VideoClip, VideoTake
    from app.services.builtin_tools._shared import _file_handle_key
    from app.services.file_access import can_read_file
    from app.services.video_studio.capture import step_result_seconds
    from app.services.video_studio.projects import can_read_project_asset, visible_render_file

    at = _number(at_seconds, "reference_at_seconds")
    step_number = _number(at_step, "reference_at_step", whole=True)
    step = None if step_number is None else int(step_number)
    crop = _crop(crop)
    named = [
        name
        for name, value in (
            ("reference_file_id", file_id),
            ("reference_recording_id", recording_id),
            ("reference_clip_id", clip_id),
        )
        if value
    ]
    if len(named) > 1:
        raise ClipError(f"A shot starts from one image or video: give one of {', '.join(named)}.")
    if at is not None and step is not None:
        raise ClipError("Give reference_at_step or reference_at_seconds, not both.")
    if step is not None and not recording_id:
        raise ClipError("reference_at_step is a step of a recording: give reference_recording_id with it.")
    if not named:
        if at is not None:
            raise ClipError("reference_at_seconds is a moment of a video: name the recording, shot or video file.")
        if crop:
            raise ClipError("crop fits a frame of a video to the shot: name the recording, shot or video file.")
        return {}

    if recording_id:
        recording_id = await _full_id(db, user, VideoTake, recording_id, noun="recordings", label=VideoTake.name)
        uid = safe_uuid(str(recording_id))
        take = await db.get(VideoTake, uid) if uid else None
        if take is None or not await can_read_project_asset(db, "take", take, user):
            raise ClipError(f"No recording `{recording_id}` you can read to start the shot from.")
        what = f"Recording `{recording_id}`"
        _usable(take, what)
        duration = (take.duration_ms or 0) / 1000
        if step is not None:
            event = next((e for e in take.events or [] if e.get("step") == step), None)
            if event is None:
                steps = sorted({e.get("step") for e in take.events or [] if isinstance(e.get("step"), int)})
                span = f"its steps are {steps[0]} to {steps[-1]}" if steps else "it has no steps"
                raise ClipError(f"{what} has no step {step}: {span}.")
            if not event.get("ok", True):
                raise ClipError(f"{what} skipped step {step}: its frames do not show it.")
            at = step_result_seconds(event, duration)
        late = at is not None and at > duration
        if late and not clamp:
            raise ClipError(f"{what} lasts {duration:.1f} s: pick a moment within it.")
        from app.services.video_studio.spec import painted_size

        size = painted_size((take.viewport_width or 0, take.viewport_height or 0), take.pixel_ratio or 1.0)
        _check_shape(what, size, aspect_ratio, crop)
        source = _video_source("recording", take.id, take.file_id, take.name, None if late else at, step, crop)
        return {"reference_video": _clamped(source, at, duration) if late else source}

    if clip_id:
        clip_id = await _full_id(db, user, VideoClip, clip_id, noun="AI shots", label=VideoClip.name)
        uid = safe_uuid(str(clip_id))
        clip = await db.get(VideoClip, uid) if uid else None
        if clip is None or not await can_read_project_asset(db, "clip", clip, user):
            raise ClipError(f"No AI shot `{clip_id}` you can read to start the shot from.")
        what = f"AI shot `{clip_id}`"
        _usable(clip, what)
        duration = (clip.duration_ms or 0) / 1000
        late = at is not None and at > duration
        if late and not clamp:
            raise ClipError(f"{what} lasts {duration:.1f} s: pick a moment within it.")
        _check_shape(what, (clip.width, clip.height), aspect_ratio, crop)
        name = clip.name or (clip.prompt or "")[:60]
        source = _video_source("shot", clip.id, clip.file_id, name, None if late else at, None, crop)
        return {"reference_video": _clamped(source, at, duration) if late else source}

    key = await _full_id(
        db, user, FileAttachment, _file_handle_key(str(file_id)), noun="files", label=FileAttachment.filename
    )
    uid = safe_uuid(str(key))
    row = await db.get(FileAttachment, uid) if uid else None
    kind = (row.content_type or "").split("/", 1)[0] if row is not None else ""
    # A render's MP4 is readable by whoever can open its video, as the media route serves it.
    readable = row is not None and kind in ("image", "video") and (
        await can_read_file(row, user, db) or (kind == "video" and await visible_render_file(db, row.id, user) is not None)
    )
    if not readable:
        raise ClipError(f"No image or video `{file_id}` you can read to start the shot from.")
    if kind == "image":
        if at is not None:
            raise ClipError("An image is one frame: reference_at_seconds picks a moment of a video.")
        if crop:
            raise ClipError("An image is sent as it is: crop fits a frame of a recording, a shot or a video file.")
        await check_start_picture(row)
        return {"reference_file_id": str(row.id)}
    return {"reference_video": _video_source("file", row.id, row.id, row.filename, at, None, crop)}


#: The params key of a shot's screen replacement (``screens.ScreenSpec`` and the start picture's size).
SCREEN = "screen"


async def resolve_screen(db: Any, user: Any, raw: Any, *, reference: dict, aspect_ratio: str) -> dict:
    """A shot's ``screen``, checked before anything is paid, as the params store it; :class:`ClipError` otherwise.

    The shot starts from a picture of the room (``reference_file_id``) of the
    shot's shape, in whose pixels the quad is given - or found, when the call
    gives none (``screens.find_screen``); the screenshot is a picture
    the person can read. The flat colour the screen is filled with is chosen
    here, once (``screens.pick_key``), so a shot generated again keeps it.
    """
    from app.services.video_studio import pictures, screens

    if reference.get("reference_video") or not reference.get("reference_file_id"):
        raise ClipError(
            "A screen replacement starts from a picture of the room with the screen in it (reference_file_id): "
            "the quad is given in that picture's pixels."
        )
    try:
        spec = screens.parse_screen(raw)
        base = await pictures.read_picture(db, user, str(reference["reference_file_id"]), what="the start picture")
        screenshot = await pictures.read_picture(db, user, spec.file_id, what="the screenshot")
        if not spec.quad:
            spec = replace(spec, quad=screens.find_screen(base), found=True)
        screens.check_fits(spec, base.size)
        # A cover fit that would cut the screenshot's content is refused here, before anything is paid.
        screens.check_content_fit(spec, screenshot)
    except (screens.ScreenError, pictures.PictureError) as exc:
        raise ClipError(str(exc)) from None
    shape = _shape(aspect_ratio)
    if shape is not None and not fits(base.size, shape):
        middle = "x".join(str(side) for side in cropped_size(base.size, shape))
        raise ClipError(
            f"The start picture of a screen replacement is {base.width}x{base.height}, not {aspect_ratio} like the "
            f"shot: the model would reframe it and the quad would no longer be the screen. Cut it to {aspect_ratio} "
            f"first (its middle {middle} keeps the most) and give the quad in the cut picture's pixels."
        )
    key = screens.pick_key(base, spec.quad)
    return {**replace(spec, key=key).as_params(), "start_size": [base.width, base.height]}


#: The params key of the flat screen a shot's start picture shows (:func:`start_surface`): its bezel, 0-1 in
#: the picture, and how flat it is. A shot set up around a screen: a take that bends it fails (:func:`take_surface`).
SURFACE = "surface"
#: The params key of what the call said of the shot's screen insert: ``True``, the shot is footage a screen
#: insert will be drawn on - its screen measured now; ``False``, it carries none - no screen is measured on its start
#: picture or its take, and no stray screen is cleaned. Unset: measured for a screen replacement, or for a shot a
#: video's scene already draws a ``screen_insert`` on (:func:`lint_surface`).
INSERT = "insert"
INSERT_OFF = "insert: false - no screen is checked on this shot's start picture or its take"
NOT_MEASURED = (
    "Screen not checked: the shot replaces no screen and no scene draws a screen insert on it (insert: true "
    "checks its start picture's screen now)"
)
#: The most videos naming a shot read to tell whether one draws a screen insert on it.
UNDER_INSERT_SCAN = 50


def insert_choice(value: Any) -> bool | None:
    """The ``insert`` a call asks: ``True``, ``False`` or ``None`` (unset); :class:`ClipError` otherwise."""
    if value is None or isinstance(value, bool):
        return value
    raise ClipError("insert is true (the shot is footage a screen insert is drawn on) or false (it carries none).")


def carries_insert(params: dict) -> bool:
    """Whether the shot of *params* is footage a screen insert is drawn on: a screen replacement, a shot asked
    ``insert: true``, or one whose start picture's screen was measured for an insert (:data:`SURFACE`) - never one
    asked ``insert: false``. Only such a shot's take is measured (:func:`take_surface`)."""
    if params.get(INSERT) is False:
        return False
    return params.get(INSERT) is True or isinstance(params.get(SCREEN), dict) or isinstance(params.get(SURFACE), dict)


async def drawn_under_insert(db: Any, user: Any, clip_id: object) -> str | None:
    """Where a video draws a screen insert on the AI shot *clip_id* - ``scene `s1` of the video `Booth``` -,
    the first of the videos naming it (``retention.uses_media``), its title said only to its owner *user*; ``None``
    when none does."""
    from sqlalchemy import select

    from app.models.video_studio import VideoProject
    from app.services.video_studio import retention
    from app.services.video_studio.spec import insert_footage

    keys = retention.media_keys([("clip", clip_id)])
    if not keys:
        return None
    rows = await db.execute(
        select(VideoProject.title, VideoProject.user_id, VideoProject.spec)
        .where(retention.uses_media(keys))
        .limit(UNDER_INSERT_SCAN)
    )
    for title, owner, spec in rows.all():
        scene = insert_footage(dict(spec or {})).get(str(clip_id))
        if scene is not None:
            return f"scene `{scene}` of the video `{title}`" if owner == getattr(user, "id", None) else f"scene `{scene}` of a video"
    return None


def _unflat(flatness: Any, what: str) -> str:
    shape = "curved" if flatness.convex else "not flat"
    return f"Regenerate {what} with a flat screen: the surface is {shape} ({flatness.fault()}) - a planar insert cannot cover it"


async def lint_surface(
    db: Any, user: Any, reference: dict, screen: dict | None, *, insert: bool | None, clip_id: object = None
) -> tuple[dict | None, list[str]]:
    """The screen a shot's start picture shows, measured only when the shot is footage a screen insert is drawn on:
    ``(its record, what the validation says)``, or :class:`ClipError` - nothing queued or paid.

    A screen replacement (*screen*), a call asking ``insert: true``, or a shot generated again (*clip_id*) that a
    video's scene draws a ``screen_insert`` on (:func:`drawn_under_insert`, read only when the picture shows a
    surface) is measured (:func:`start_surface`): a curved surface is refused, one seen at an angle is said as a fact.
    Any other shot has no surface to be flat: nothing is measured, and the notes say so.
    """
    from app.services.video_studio.surface_refine import Flatness

    if not reference.get("reference_file_id"):
        return None, []
    if insert is False:
        return None, [f"{INSERT_OFF}."]
    asked = screen is not None or insert is True
    if not asked and clip_id is None:
        return None, [f"{NOT_MEASURED}."]
    surface = await start_surface(db, user, reference, screen)
    under = None
    if not asked:
        if surface is None:
            return None, []
        under = await drawn_under_insert(db, user, clip_id)
        if under is None:
            return None, [f"{NOT_MEASURED}."]
    if surface is None:
        # A screen replacement's quad stands for its surface: none here is a picture this check could not read.
        return None, [] if screen else ["No screen found on the start picture to measure: it shows no flat green, blue or magenta surface."]
    shape = Flatness.from_dict(surface["flatness"])
    if not shape.flat:
        why = f" ({under} draws a screen insert on this shot)" if under else ""
        raise ClipError(f"{_unflat(shape, 'the start picture')}{why}. Nothing was queued or paid.")
    seen = shape.seen_at()
    if seen is None:
        return surface, []
    return surface, [f"The start picture's screen is {seen}: a fact, not a fault - flat, the insert is drawn onto it exactly."]


async def start_surface(db: Any, user: Any, reference: dict, screen: dict | None) -> dict | None:
    """The screen a shot's start picture shows, measured before anything is paid: its bezel and flatness
    (``surface_refine.Bezel.as_dict``), or ``None`` - a shot from no picture, or one showing no flat green, blue or
    magenta surface and replacing no screen. :func:`lint_surface` decides whether it is measured and judges it.

    A screen replacement's quad (*screen*, the params ``resolve_screen`` built) is its seed. One seen at an angle is
    flat: the homography covers it.
    """
    import asyncio

    import numpy as np

    from app.services.video_studio import pictures, screens, surface_refine

    if not reference.get("reference_file_id"):
        return None
    try:
        picture = await pictures.read_picture(db, user, str(reference["reference_file_id"]), what="the start picture")
        pixels = np.asarray(picture.convert("RGB"))
        given = screens.start_quad(screen) if screen else None
        bezel = await asyncio.to_thread(surface_refine.measure_bezel, pixels, given=given)
    except Exception as exc:  # a picture that cannot be read here is checked where it is used
        logger.info("Start picture surface not measured: %s", format_exception_message(exc))
        return None
    if bezel is None and given is not None:
        # A screen whose edges give no line: the quad given is the surface.
        height, width = pixels.shape[:2]
        corners = [(x * width, y * height) for x, y in given]
        bezel = surface_refine.Bezel(tuple(given), surface_refine.flatness(corners, size=(width, height)), None, "given")
    return None if bezel is None else bezel.as_dict()


async def take_surface(path: str, workdir: str, params: dict) -> tuple[dict | None, str | None]:
    """The flat screen the first frame of the take at *path* shows: ``(its record, why it is not flat)``.

    Measured on the take as made, before a screenshot is drawn into it, of a
    shot that carries a screen insert (:func:`carries_insert`): seeded
    on the shot's screen replacement (``params.screen``) or on the screen its
    start picture showed (:data:`SURFACE`), else the flat key-coloured surface
    the frame shows, if any. ``(None, None)``: no screen there, or a frame that
    cannot be read (the take's other checks stand).
    """
    import asyncio
    from pathlib import Path

    import numpy as np
    from PIL import Image

    from app.services.video_studio import encoder, screens, surface_refine

    out = Path(workdir) / "surface-first.png"
    try:
        await encoder.extract_frame(path, at=0.0, out_path=str(out))
        with Image.open(out) as image:
            pixels = np.asarray(image.convert("RGB"))
    except Exception as exc:  # advisory: the take is judged by its other checks
        logger.info("Take surface not measured: %s", format_exception_message(exc))
        return None, None
    screen = params.get(SCREEN) if isinstance(params.get(SCREEN), dict) else None
    started = params.get(SURFACE) if isinstance(params.get(SURFACE), dict) else None
    given = screens.start_quad(screen) if screen else (tuple(tuple(p) for p in started["quad"]) if started else None)
    key = screens.ScreenSpec.from_params(screen).key if screen else None
    bezel = await asyncio.to_thread(surface_refine.measure_bezel, pixels, key, given=given)
    if bezel is None:
        return None, None
    record = bezel.as_dict()
    return record, None if bezel.flatness.flat else _unflat(bezel.flatness, "the shot")


def screen_again(stored: dict) -> dict:
    """A stored screen as it is checked against another start picture: a quad found in the old one is found again."""
    return {key: value for key, value in stored.items() if key != "quad"} if stored.get("found") else stored


def with_screen(params: dict, prompt: str, negative_prompt: str | None) -> tuple[str, str | None]:
    """*prompt* and *negative_prompt* as a screen shot's model reads them: its screen asked flat."""
    from app.services.video_studio import screens

    stored = params.get(SCREEN)
    key = screens.ScreenSpec.from_params(stored).key if isinstance(stored, dict) else None
    if key is None:
        return prompt, negative_prompt
    return screens.provider_prompt(prompt, key)[:4000], screens.provider_negative(negative_prompt)


# ── A term the takes keep drawing against the negative prompt ─────

#: The params key of the negative prompt's terms each content check found a take showing: ``{"attempt": n, "terms":
#: [...]}`` per take checked, newest last (the last :data:`INSIST_AFTER` kept). Keyed by the take's attempt, so a take
#: whose content check never ran - it failed an earlier check - breaks the run of takes in a row.
EXCLUDED_SHOWN = "excluded_shown"
#: The params key of the terms the next take asks in its prompt itself, the model it is asked of and the guidance it
#: lowers where that model has one: ``{"terms": [...], "model": m, "guidance": g?}``, decided when the shot is
#: generated again (:func:`plan_restart`).
INSISTED = "insisted"
#: A term the content check found on this many takes in a row is asked in the prompt the next time ...
INSIST_AFTER = 2
#: ... at most this many terms, each at most this long: the prompt's own words always stay whole.
MAX_INSISTED_TERMS = 3
MAX_INSISTED_TERM_CHARS = 80


def _attempt(params: dict) -> int:
    try:
        return int(params.get("attempt") or 0)
    except (TypeError, ValueError):
        return 0


def record_excluded_shown(params: dict, terms: list[str]) -> None:
    """Keep in *params* the negative prompt's *terms* this take's content check failed on (``[]``: none, it passed),
    under the take's attempt - a take checked again (a run resumed) replaces its own entry."""
    attempt = _attempt(params)
    history = [entry for entry in params.get(EXCLUDED_SHOWN) or [] if isinstance(entry, dict) and entry.get("attempt") != attempt]
    params[EXCLUDED_SHOWN] = [*history, {"attempt": attempt, "terms": list(dict.fromkeys(terms))}][-INSIST_AFTER:]


def insistent_terms(params: dict, negative_prompt: str | None, *, attempt: int) -> list[str]:
    """The terms of *negative_prompt*, in its order, the content checks of the :data:`INSIST_AFTER` takes right before
    *attempt* all failed on - none when one of those takes was never checked - at most :data:`MAX_INSISTED_TERMS`."""
    from app.services.video_studio.content_drift import excluded_terms

    checked = {entry.get("attempt"): entry for entry in params.get(EXCLUDED_SHOWN) or [] if isinstance(entry, dict)}
    previous = [checked.get(attempt - back) for back in range(1, INSIST_AFTER + 1)]
    if any(entry is None for entry in previous):
        return []
    shown = set.intersection(*({str(term).lower() for term in entry.get("terms") or []} for entry in previous))
    kept = [term for term in excluded_terms(negative_prompt) if term.lower() in shown and len(term) <= MAX_INSISTED_TERM_CHARS]
    return kept[:MAX_INSISTED_TERMS]


def lowered_guidance(provider: Any, model: str, params: dict) -> float | None:
    """The guidance a take asking its terms in the prompt is sent with: halfway from where it stood - the last lowering
    for this very *model*, else its default - to its lowest, within the model's own range; ``None`` for a model
    without one (``video_guidance``). A lowering for another model (a fallback chain) is on that model's scale."""
    scale = provider.video_guidance(model) if provider is not None else None
    if scale is None:
        return None
    lowest, default = sorted(float(value) for value in scale)
    before = params.get(INSISTED) or {}
    kept = before.get("guidance")
    start = float(kept) if before.get("model") == model and isinstance(kept, int | float) else default
    return round(min(default, max(lowest, start - (start - lowest) / 2.0)), 3)


def insisted_prompts(params: dict, prompt: str, negative_prompt: str | None) -> tuple[str, str | None]:
    """*prompt* and *negative_prompt* with the shot's insisted terms (:data:`INSISTED`) moved into the prompt itself:
    "No X anywhere in the shot." for each, and the term out of the negative prompt."""
    from app.services.video_studio.content_drift import without_terms

    terms = [str(term) for term in (params.get(INSISTED) or {}).get("terms") or []][:MAX_INSISTED_TERMS]
    if not terms:
        return prompt, negative_prompt
    said = " ".join(f"No {term} anywhere in the shot." for term in terms)
    room = max(0, MAX_PROMPT_CHARS - len(said) - 1)
    return f"{prompt.rstrip()[:room]} {said}".strip(), without_terms(negative_prompt, terms)


def insisted_guidance(params: dict, provider: Any, model: str) -> float | None:
    """The guidance the take is sent with (``VideoRequest.guidance``): the lowered one, for the model it was lowered
    for, while that model has one."""
    insisted = params.get(INSISTED) or {}
    guidance = insisted.get("guidance")
    if not isinstance(guidance, int | float) or insisted.get("model") != model:
        return None
    if provider is None or provider.video_guidance(model) is None:
        return None
    return float(guidance)


def insisted_note_of(params: dict, guidance: float | None, model: str) -> str | None:
    """The note of a take asked with its insisted terms (:func:`insisted_note`); ``None`` for a take asked as written."""
    terms = [str(term) for term in (params.get(INSISTED) or {}).get("terms") or []]
    return insisted_note(terms, guidance, model) if terms else None


def insisted_note(terms: list[str], guidance: float | None, model: str) -> str:
    """What a shot generated again says it did about the terms its last takes kept drawing."""
    listed = ", ".join(terms)
    said = " ".join(f'"No {term} anywhere in the shot."' for term in terms)
    lowered = (
        f" and its guidance lowered to {guidance:g}" if guidance is not None else f" ({model} has no guidance setting to lower)"
    )
    return (
        f"The last {INSIST_AFTER} takes showed {listed} against the negative prompt: this one asks it in the prompt "
        f"itself ({said}), out of the negative prompt{lowered}."
    )


#: The most reference images a shot is asked with, whatever its model takes beyond.
MAX_REFERENCE_IMAGES = 3
#: The params key of a shot's reference images: the same files are sent on every generation of it.
REFERENCE_IMAGES = "reference_images"


def _reference_ids(values: Any) -> list[str]:
    """The reference image ids a call names (a list, or one id), in order, once each."""
    from app.services.builtin_tools._shared import _file_handle_key

    if values in (None, ""):
        return []
    items = values if isinstance(values, list | tuple) else [values]
    return list(dict.fromkeys(_file_handle_key(str(item)).strip() for item in items if str(item or "").strip()))


def check_reference_combination(
    limits: dict, model: str, *, references: int, start_frame: bool, again: bool = False
) -> None:
    """Refuse *references* reference images *model* cannot take as asked, naming it and the ways out.

    *limits* is what its provider declares (``router.video_reference_limits``):
    how many it takes, and whether beside a start frame. *again*: a shot
    generated again, whose ways out are its own arguments.
    """
    if not references:
        return
    if references > MAX_REFERENCE_IMAGES:
        raise ClipError(f"A shot takes at most {MAX_REFERENCE_IMAGES} reference images.")
    limit = int(limits.get("reference_images") or 0)
    if limit <= 0:
        raise ClipError(
            f"{model} takes no reference images: video_studio_capabilities lists the models that do "
            "(reference_images), or start the shot from a picture instead (reference_file_id)",
            about_model=True,
        )
    if references > limit:
        raise ClipError(f"{model} takes at most {limit} reference images.", about_model=True)
    if start_frame and not limits.get("reference_images_with_start_frame"):
        if again:
            ways = (
                "start_frame: false drops the shot's start frame and keeps the reference images, for a subject that "
                "must stay the same; start_frame: true with the reference_* of the picture to start from (and no "
                "reference_image_file_ids) drops them and keeps the start frame, for a shot that continues a picture"
            )
        else:
            ways = (
                "leave out the reference_* start to keep the reference images, for a subject that must stay the "
                "same; leave out reference_image_file_ids to keep the start frame, for a shot that continues a picture"
            )
        raise ClipError(f"{model} takes reference images or a start frame, not both in one shot: {ways}", about_model=True)


async def resolve_reference_images(
    db: Any,
    user: Any,
    file_ids: Any,
    *,
    provider: Any,
    model: str,
    start_frame: bool,
    again: bool = False,
    fit: str | None = None,
    notes: list[str] | None = None,
) -> list[str]:
    """The reference images of a shot, checked before anything is paid for, or :class:`ClipError`.

    Pictures the shot must keep - a character, a garment, a product, a logo -
    sent beside the prompt to a model whose provider takes them
    (``max_reference_images``), refused with the model's name otherwise, and
    with a start frame only where the model takes both in one request
    (:func:`check_reference_combination`, before any file is read). Each is a
    picture the person can read, one a provider can read (:func:`check_start_picture`),
    of a shape the model takes (:func:`check_reference_shape`: refused, or padded with *fit*).
    """
    from app.core.utils import safe_uuid
    from app.llm.router import video_reference_limits
    from app.models.file_attachment import FileAttachment
    from app.services.file_access import can_read_file

    ids = _reference_ids(file_ids)
    if not ids:
        return []
    check_reference_combination(
        video_reference_limits(provider, model), model, references=len(ids), start_frame=start_frame, again=again
    )
    kept: list[str] = []
    for file_id in ids:
        uid = safe_uuid(file_id)
        row = await db.get(FileAttachment, uid) if uid else None
        if (
            row is None
            or not (row.content_type or "").startswith("image/")
            or not await can_read_file(row, user, db)
        ):
            raise ClipError(f"No picture `{file_id}` you can read to use as a reference image.")
        picture = await check_start_picture(row)
        await check_reference_shape(row, picture, provider=provider, model=model, fit=fit, notes=notes)
        kept.append(str(row.id))
    return kept


async def reference_pictures(params: dict, workdir: str) -> list[tuple[bytes, str]]:
    """The reference images a shot's params name, each as every provider reads it (:func:`start_picture`).

    Read on every generation, from the same files: a shot generated
    again keeps its subjects. One gone since fails the shot rather than make
    it without its reference.
    """
    from app.services.video_studio import assets

    pictures: list[tuple[bytes, str]] = []
    for index, file_id in enumerate(params.get(REFERENCE_IMAGES) or []):
        path = Path(workdir) / f"reference-image-{index}"
        row = await assets.download_file(file_id, str(path))
        if row is None:
            raise ClipError("A reference image of this shot is gone: generate it again with another one.")
        picture = await start_picture(path.read_bytes(), row.content_type)
        pictures.append((picture.data, picture.mime))
    return pictures


async def fit_reference_pictures(
    params: dict, pictures: list[tuple[bytes, str]], limits: Any, *, model: str
) -> list[tuple[bytes, str]]:
    """*pictures* (:func:`reference_pictures`) fitted to *model*'s *limits* as the shot asked, or :class:`ClipError`.

    Checked again at the worker, before the provider is asked: a shot planned
    for another model, or a picture changed since, is padded
    (:data:`REFERENCE_FIT` ``pad``) or refused with nothing paid.
    """
    if limits is None:
        return pictures
    fit = params.get(REFERENCE_FIT) or FIT_REFUSE
    names = [str(item) for item in params.get(REFERENCE_IMAGES) or []]
    fitted: list[tuple[bytes, str]] = []
    for index, (data, mime) in enumerate(pictures):
        name = names[index] if index < len(names) else f"reference image {index + 1}"
        sent, sent_mime, _note = await fit_reference(data, mime, limits, fit=fit, name=name, model=model)
        fitted.append((sent, sent_mime))
    return fitted


# ── The shape of a reference image ──────────────────────────────

#: The params key of how a shot's reference images are fitted to its model's limits: ``refuse`` (unset) or ``pad``.
REFERENCE_FIT = "reference_fit"
#: A picture outside the model's limits is refused before anything is paid.
FIT_REFUSE = "refuse"
#: A picture outside the model's ratio is padded on transparent to the nearest accepted one (every pixel kept), and
#: scaled to its accepted sides.
FIT_PAD = "pad"
_FITS = (FIT_REFUSE, FIT_PAD)


def reference_fit_choice(value: Any) -> str | None:
    """``reference_fit`` as asked: ``refuse``, ``pad``, ``None`` unset, or :class:`ClipError`."""
    if value in (None, ""):
        return None
    if value not in _FITS:
        raise ClipError(
            'reference_fit is "refuse" (a reference image outside the model\'s accepted shape is refused) or "pad" '
            "(it is padded on transparent to the nearest accepted shape, every pixel kept)."
        )
    return str(value)


def _fitted_size(size: tuple[int, int], limits: Any) -> tuple[int, int, float]:
    """The canvas a *size* picture is padded onto to reach *limits*' ratio, and the scale its sides then need."""
    width, height = size
    ratio = width / height
    if ratio > limits.max_ratio:
        height = math.ceil(width / limits.max_ratio)
    elif ratio < limits.min_ratio:
        width = math.ceil(height * limits.min_ratio)
    scale = 1.0
    if limits.min_side and min(width, height) < limits.min_side:
        scale = limits.min_side / min(width, height)
    if limits.max_side and max(width, height) * scale > limits.max_side:
        scale = limits.max_side / max(width, height)
    return width, height, scale


def reference_misfit(size: tuple[int, int], limits: Any) -> str | None:
    """What is wrong with a *size* picture for *limits* (its ratio, a side), ``None`` when it fits them."""
    from app.llm.video import ratio_label

    width, height = size
    ratio = width / height
    if not limits.min_ratio <= ratio <= limits.max_ratio:
        return f"is {width}x{height} ({ratio_label(ratio)}), outside the accepted aspect ratio {limits.ratio_words()}"
    if limits.min_side and min(size) < limits.min_side:
        return f"is {width}x{height}, a side under the {limits.min_side} px the model takes"
    if limits.max_side and max(size) > limits.max_side:
        return f"is {width}x{height}, a side over the {limits.max_side} px the model takes"
    return None


def _pad(data: bytes, limits: Any) -> tuple[bytes, tuple[int, int], tuple[int, int]]:
    """*data* padded on transparent to *limits* (centred, every pixel kept), as PNG: ``(png, before, after)``."""
    import io

    from PIL import Image

    with Image.open(io.BytesIO(data)) as image:
        image.load()
        picture = image.convert("RGBA")
    before = picture.size
    width, height, scale = _fitted_size(before, limits)
    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    canvas.paste(picture, ((width - before[0]) // 2, (height - before[1]) // 2))
    if scale != 1.0:
        canvas = canvas.resize((max(1, round(width * scale)), max(1, round(height * scale))), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    canvas.save(out, format="PNG")
    return out.getvalue(), before, canvas.size


async def fit_reference(
    data: bytes, mime: str, limits: Any, *, fit: str, name: str, model: str | None, svg: bool = False
) -> tuple[bytes, str, str | None]:
    """A reference picture (already a PNG or a JPEG, :func:`start_picture`) fitted to *limits*, or :class:`ClipError`.

    ``(data, mime, note)``: as it is when it fits them (or *limits* is ``None``:
    nothing documented, nothing refused); padded on transparent to the nearest
    accepted ratio when *fit* is :data:`FIT_PAD`, the note saying so; refused
    otherwise, naming the picture, its shape, the accepted range and the fix.
    """
    import asyncio

    from app.llm.video import ratio_label

    if limits is None:
        return data, mime, None
    size = image_size_of(data)
    if size is None:
        raise StartPictureError(f"The reference image `{name}` could not be read as an image.")
    wrong = reference_misfit(size, limits)
    if wrong is None:
        return data, mime, None
    drawn = " (an SVG, rasterised to PNG first)" if svg else ""
    for_model = f" for {model}" if model else ""
    if fit != FIT_PAD:
        raise ClipError(
            f"The reference image `{name}`{drawn} {wrong}{for_model} (accepted: aspect ratio {limits.ratio_words()}"
            f"{f', sides of {limits.min_side} px or more' if limits.min_side else ''}"
            f"{f', at most {limits.max_side} px' if limits.max_side else ''}): nothing was paid. Pass "
            'reference_fit: "pad" to pad it on transparent to the nearest accepted shape (every pixel kept), or give '
            "a picture of an accepted shape."
        )
    padded, before, after = await asyncio.to_thread(_pad, data, limits)
    note = (
        f"Reference image `{name}`{drawn} padded from {ratio_label(before[0] / before[1])} ({before[0]}x{before[1]}) to "
        f"{ratio_label(after[0] / after[1])} ({after[0]}x{after[1]}){for_model}."
    )
    return padded, "image/png", note


def image_size_of(data: bytes) -> tuple[int, int] | None:
    """The pixel size of the picture *data*; ``None`` when it is not one."""
    import io

    from PIL import Image

    try:
        with Image.open(io.BytesIO(data)) as picture:
            return picture.size
    except OSError:  # Pillow's UnidentifiedImageError included
        return None


def reference_limits_of(provider: Any, model: str) -> Any:
    """What *model* takes as a reference image (``ReferenceImageLimits``), ``None`` where nothing is documented."""
    declared = getattr(provider, "reference_image_limits", None)
    return declared(model) if callable(declared) else None


async def check_reference_shape(
    row: Any, picture: StartFrame | None, *, provider: Any, model: str, fit: str | None, notes: list[str] | None
) -> None:
    """Refuse, before anything is paid, a reference picture *model* would refuse on its shape.

    *picture* is the file *row* as it is sent (an SVG rasterised first,
    :func:`check_start_picture`), measured against what the model declares
    (``reference_image_limits``); with ``reference_fit: "pad"`` it is padded
    instead, said in *notes*.
    """
    limits = reference_limits_of(provider, model)
    if limits is None or picture is None:
        return
    _data, _mime, note = await fit_reference(
        picture.data, picture.mime, limits, fit=fit or FIT_REFUSE, name=str(row.id), model=model, svg=picture.svg
    )
    if note and notes is not None:
        notes.append(note)
    elif picture.svg and notes is not None:
        notes.append(f"Reference image `{row.id}` is an SVG: rasterised to PNG before it is checked and sent.")


def reference_note(params: dict) -> str | None:
    """How a shot was asked to keep its subjects, as its notes say it; ``None`` for nothing to keep."""
    kept = ingredients_of(params)
    if kept:
        names = ", ".join(item.name for item in kept)
        if params.get(INGREDIENTS_PATH) == PATH_PROMPT:
            return f"Ingredients ({names}) named in the prompt: the model takes no reference images with this shot."
        if params.get(INGREDIENTS_PATH) == PATH_COMPOSITED:
            return (
                f"Ingredients ({names}) named in the prompt, the logo drawn back with its own pixels once the shot is "
                "made: the model takes no reference images with this shot."
            )
        if params.get(INGREDIENTS_PATH) == PATH_TRACKED:
            subjects = ", ".join(item.subject.name for item in kept if item.subject is not None)
            return (
                f"Ingredients ({names}) drawn with their own pixels onto their subject ({subjects}) wherever the take "
                "shows it, once the shot is made: the model takes no picture of them with this shot."
            )
        return f"Ingredients ({names}) sent as the model's reference images."
    count = len(params.get(REFERENCE_IMAGES) or [])
    if not count:
        return None
    return f"Asked with {count} reference image{'s' if count > 1 else ''} to keep."


#: How a shot is asked to look like something: it continues a picture, keeps subjects, both, or neither.
MODE_START_FRAME = "start_frame"
MODE_REFERENCE_IMAGES = "reference_images"
MODE_BOTH = "start_frame_and_reference_images"
MODE_PROMPT = "prompt_only"
_MODE_LABELS = {
    MODE_START_FRAME: "start frame only (the shot continues its start picture)",
    MODE_REFERENCE_IMAGES: "reference images only (the shot keeps their subjects, no start frame)",
    MODE_BOTH: "start frame and reference images",
    MODE_PROMPT: "prompt only (no start frame, no reference images)",
}


def request_mode(params: dict) -> str:
    """Which of the modes a shot's params ask it in: the reference images the model is sent, its start frame."""
    start, references = starts_from_picture(params), bool(params.get(REFERENCE_IMAGES))
    if start and references:
        return MODE_BOTH
    if start:
        return MODE_START_FRAME
    return MODE_REFERENCE_IMAGES if references else MODE_PROMPT


def mode_note(params: dict) -> str:
    """The sentence an answer says the mode in."""
    return f"Mode: {_MODE_LABELS[request_mode(params)]}."


# ── Ingredients ───────────────────────────────────────────────────

#: What an ingredient of a shot is: what the prompt calls it when the model cannot take its picture.
INGREDIENT_ROLES = ("logo", "product", "character")
#: The params keys: the ingredients as asked, and how they reached the model.
INGREDIENTS = "ingredients"
INGREDIENTS_PATH = "ingredients_path"
#: Through the model's own reference mechanism (Veo reference images, Kling Omni, Runway references).
PATH_NATIVE = "native"
#: Named in the prompt, the shot's start frame showing them: the model has no way to take the pictures.
PATH_PROMPT = "prompt"
#: Named in the prompt, and every logo's own pixels drawn back onto the made shot (``logos``).
PATH_COMPOSITED = "composited"
#: Every logo's own pixels drawn onto its subject wherever the take shows it (``tracked_logos``): the model
#: takes no picture of it, and the start frame need not show the subject.
PATH_TRACKED = "tracked"
#: The paths whose ingredients are named in the prompt, the start frame showing them.
_PROMPT_PATHS = (PATH_PROMPT, PATH_COMPOSITED, PATH_TRACKED)
_LABEL_MAX = 80


@dataclass(frozen=True)
class Subject:
    """The subject a logo sits on: a picture of it wearing the logo, and what to call it."""

    file_id: str
    label: str | None = None

    @property
    def name(self) -> str:
        return self.label or "the subject"

    def as_dict(self) -> dict:
        return {"file_id": self.file_id, **({"label": self.label} if self.label else {})}


@dataclass(frozen=True)
class Ingredient:
    """A picture a shot keeps - a logo, a product, a character - with what to call it.

    A logo may say where it is in the start frame (*quad*, 0-1);
    otherwise it is found there. A logo on a subject the start frame does not
    show names that subject instead (*subject*): the logo is drawn onto
    it wherever the take shows it.
    """

    file_id: str
    role: str
    label: str | None = None
    quad: tuple[tuple[float, float], ...] | None = None
    subject: Subject | None = None

    @property
    def name(self) -> str:
        return self.label or f"the {self.role}"

    @property
    def tracked(self) -> bool:
        """Whether the ingredient is drawn onto a subject found in the take: it needs no start frame showing it."""
        return self.role == "logo" and self.subject is not None

    def as_dict(self) -> dict:
        return {
            "file_id": self.file_id,
            "role": self.role,
            **({"label": self.label} if self.label else {}),
            **({"quad": [list(p) for p in self.quad]} if self.quad else {}),
            **({"subject": self.subject.as_dict()} if self.subject else {}),
        }


def _ingredient_quad(raw: Any, index: int) -> tuple[tuple[float, float], ...]:
    """A logo ingredient's ``quad``: four corners 0-1 across and down the start frame, convex, in order."""
    from app.services.video_studio.spec import quad_problems

    shaped = isinstance(raw, list) and len(raw) == 4 and all(isinstance(p, list | tuple) and len(p) == 2 for p in raw)
    try:
        quad = tuple((float(p[0]), float(p[1])) for p in raw) if shaped else ()
    except (TypeError, ValueError):
        quad = ()
    if not quad or not all(-0.25 <= v <= 1.25 for p in quad for v in p):
        raise ClipError(
            f"ingredients.{index}.quad is the logo's four corners [x, y], 0-1 across and down the start frame: top "
            "left, top right, bottom right, bottom left."
        )
    problems = quad_problems(list(quad))
    if problems:
        raise ClipError(f"ingredients.{index}.quad: {problems[0]}.")
    return quad


def _label(raw: Any) -> str | None:
    return re.sub(r"\s+", " ", str(raw or "")).strip()[:_LABEL_MAX] or None


def _ingredient_subject(raw: Any, index: int, role: str, file_handle_key: Callable[[str], str]) -> Subject:
    """A logo ingredient's ``subject``: the picture of the subject wearing it, and what to call the subject."""
    if role != "logo":
        raise ClipError(f"ingredients.{index}.subject names the subject a logo sits on: a {role} is kept by the model.")
    if not isinstance(raw, dict) or not str(raw.get("file_id") or "").strip():
        raise ClipError(
            f"ingredients.{index}.subject is {{file_id, label?}}: a picture of the subject wearing the logo (a "
            "conversation file, or a frame saved from another shot) and, if you like, what to call it."
        )
    return Subject(file_handle_key(str(raw["file_id"])).strip(), _label(raw.get("label")))


def parse_ingredients(raw: Any) -> list[Ingredient]:
    """The ingredients a call names, checked field by field, or :class:`ClipError`."""
    from app.services.builtin_tools._shared import _file_handle_key

    if raw in (None, ""):
        return []
    if not isinstance(raw, list):
        raise ClipError("ingredients is a list of {file_id, role, label?}.")
    if len(raw) > MAX_REFERENCE_IMAGES:
        raise ClipError(f"A shot takes at most {MAX_REFERENCE_IMAGES} ingredients.")
    found: list[Ingredient] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict) or not str(item.get("file_id") or "").strip():
            raise ClipError(f"ingredients.{index}: an ingredient names its picture (file_id).")
        role = str(item.get("role") or "").strip()
        if role not in INGREDIENT_ROLES:
            raise ClipError(f"ingredients.{index}.role is one of {', '.join(INGREDIENT_ROLES)}.")
        label = _label(item.get("label"))
        quad = None
        if item.get("quad") is not None:
            if role != "logo":
                raise ClipError(f"ingredients.{index}.quad places a logo: a {role} is kept by the model, not drawn back.")
            quad = _ingredient_quad(item["quad"], index)
        subject = None
        if item.get("subject") is not None:
            subject = _ingredient_subject(item["subject"], index, role, _file_handle_key)
            if quad is not None:
                raise ClipError(
                    f"ingredients.{index}: give quad or subject, not both - the quad places a logo in the start frame, "
                    "the subject is for a logo on a subject the start frame does not show."
                )
        found.append(Ingredient(_file_handle_key(str(item["file_id"])).strip(), role, label, quad, subject))
    if len({item.file_id for item in found}) != len(found):
        raise ClipError("Each ingredient is a different picture.")
    logos = [item for item in found if item.role == "logo"]
    if logos and any(item.tracked for item in logos) and not all(item.tracked for item in logos):
        raise ClipError(
            "Give every logo its subject, or none: a shot draws its logos back from the start frame (ingredients_path "
            "composited) or onto a subject found in the take (tracked), not both."
        )
    return found


def _subject_of(raw: Any) -> Subject | None:
    if not isinstance(raw, dict) or not raw.get("file_id"):
        return None
    return Subject(str(raw["file_id"]), raw.get("label") or None)


def ingredients_of(params: dict) -> list[Ingredient]:
    """The ingredients a shot's params keep, as asked."""
    return [
        Ingredient(
            str(item["file_id"]),
            str(item.get("role") or "product"),
            item.get("label"),
            tuple((float(x), float(y)) for x, y in item["quad"]) if isinstance(item.get("quad"), list) else None,
            _subject_of(item.get("subject")),
        )
        for item in params.get(INGREDIENTS) or []
        if isinstance(item, dict) and item.get("file_id")
    ]


def ingredient_prompt(prompt: str, ingredients: list[Ingredient]) -> str:
    """*prompt* with each ingredient named, for a model that cannot take their pictures: the start frame shows them.

    A logo drawn onto its subject is not named: no picture the model
    sees shows it, and it is drawn once the shot is made.
    """
    asked = " ".join(f"Keep {item.name} exactly as in the reference image." for item in ingredients if not item.tracked)
    return f"{prompt.rstrip()} {asked}".strip() if asked else prompt


def with_ingredients(prompt: str, params: dict) -> str:
    """*prompt* as the model reads it: the ingredients named when they go through the prompt."""
    if params.get(INGREDIENTS_PATH) in _PROMPT_PATHS:
        return ingredient_prompt(prompt, ingredients_of(params))
    return prompt


def composited_logos(params: dict) -> list[Ingredient]:
    """The logos of a shot whose ingredients are composited: drawn back onto every frame once it is made."""
    if params.get(INGREDIENTS_PATH) != PATH_COMPOSITED:
        return []
    return [item for item in ingredients_of(params) if item.role == "logo"]


def tracked_logos(params: dict) -> list[Ingredient]:
    """The logos of a shot on the tracked path: drawn onto their subject wherever the made take shows it."""
    if params.get(INGREDIENTS_PATH) != PATH_TRACKED:
        return []
    return [item for item in ingredients_of(params) if item.tracked]


def native_logos_with_subject(params: dict) -> list[Ingredient]:
    """The logos a native take was sent with their subject named: checked on the take, tracked when it fails."""
    if params.get(INGREDIENTS_PATH) != PATH_NATIVE:
        return []
    return [item for item in ingredients_of(params) if item.tracked]


def needs_start_frame(ingredients: list[Ingredient], path: str) -> bool:
    """Whether *ingredients* on *path* need a start frame: one the prompt points at, for an ingredient the model is told to keep.

    A logo drawn onto its subject is kept by the code once the take is
    made, from the subject's picture: it needs none.
    """
    return path != PATH_NATIVE and any(not item.tracked for item in ingredients)


def provider_prompts(params: dict, prompt: str, negative_prompt: str | None) -> tuple[str, str | None]:
    """*prompt* and *negative_prompt*, the art direction applied, as the shot's model is sent them: the ingredients
    named, a screen asked flat, a screen insert's rectangle kept clear (``insert_keep_out.with_line``), the
    start picture's printed words held (:func:`with_lettering`), the terms the last takes kept drawing asked in the
    prompt itself."""
    from app.services.video_studio.insert_keep_out import with_line

    prompt, negative = with_screen(params, consistency_prompt(with_ingredients(prompt, params), params), negative_prompt)
    prompt, negative = with_lettering(params, with_line(params, prompt), negative)
    return insisted_prompts(params, prompt, negative)


def lettering_words(params: dict) -> list[str]:
    """The printed words the start picture was read to show at plan time (``text_drift.START_LETTERING_KEY``)."""
    from app.services.video_studio import text_drift

    kept = params.get(text_drift.START_LETTERING_KEY)
    return [str(w) for w in (kept.get("words") or []) if str(w).strip()] if isinstance(kept, dict) else []


def lettering_regions(params: dict) -> list[dict]:
    """The start picture's text regions read at plan time (``text_drift.text_regions``): ``{text, box, held}``."""
    from app.services.video_studio import text_drift

    kept = params.get(text_drift.START_LETTERING_KEY)
    regions = kept.get("regions") if isinstance(kept, dict) else None
    return [dict(r) for r in regions if isinstance(r, dict) and r.get("box")] if isinstance(regions, list) else []


def with_lettering(params: dict, prompt: str, negative_prompt: str | None) -> tuple[str, str | None]:
    """*prompt* ending with the lettering line of the start picture's words ("Every printed word stays exactly: ..."),
    within :data:`MAX_PROMPT_CHARS`, and *negative_prompt* naming new lettering (``text_drift.NEGATIVE_LETTERING``);
    both as given for a shot whose start picture reads no word."""
    from app.services.video_studio import text_drift

    words = lettering_words(params)
    line = text_drift.lettering_line(words)
    if line is None:
        return prompt, negative_prompt
    room = max(0, MAX_PROMPT_CHARS - len(line) - 1)
    return f"{prompt.rstrip()[:room]} {line}".strip(), text_drift.with_negative_lettering(negative_prompt)


def _covered(box: tuple[float, float, float, float], regions: list[list[float]] | None) -> bool:
    """Whether *box* (shares) lies inside one of the *regions* a take keeps from its start picture (``keep_regions``)."""
    x, y, w, h = box
    for region in regions or []:
        rx, ry, rw, rh = (float(v) for v in region)
        if rx - 0.01 <= x and ry - 0.01 <= y and x + w <= rx + rw + 0.01 and y + h <= ry + rh + 0.01:
            return True
    return False


async def lint_lettering(
    db: Any, user: Any, reference: dict, *, text_check: bool, regions: list[list[float]] | None
) -> tuple[dict | None, list[str]]:
    """The lettering a shot's start picture shows, read before anything is paid (``text_drift.read_start_picture``):
    ``(what it reads, as the params keep it, what the validation says)``, or :class:`ClipError`.

    Gibberish already in the plate - letters that make no word, no brand term, no number - is refused naming its box,
    unless a ``keep_regions`` box covers it (the take draws the picture's own pixels back there). Read for a shot
    made from a picture (``reference_file_id``) with its text check on; a picture or a reading that cannot be had
    leaves the shot asked as before (logged): the prompt then holds no lettering line, the take's text check stands.
    """
    import io

    from app.services.video_studio import pictures, text_drift

    if not reference.get("reference_file_id") or not text_check:
        return None, []
    try:
        picture = await pictures.read_picture(db, user, str(reference["reference_file_id"]), what="the start picture")
        out = io.BytesIO()
        picture.convert("RGB").save(out, format="PNG")
        lettering = await text_drift.read_start_picture(getattr(user, "id", user), (out.getvalue(), "image/png"))
    except Exception as exc:  # the picture is checked where it is used; the shot is asked as before
        logger.info("Start picture lettering not read: %s", format_exception_message(exc))
        return None, []
    if lettering is None:
        return None, []
    uncovered = [item for item in lettering.invented if item.box is None or not _covered(item.box, regions)]
    if uncovered:
        refused = text_drift.StartLettering(lettering.words, uncovered).refusal
        raise ClipError(f"{refused} Nothing was queued or paid.")
    notes: list[str] = []
    if lettering.words:
        shown = ", ".join(f'"{w}"' for w in lettering.words[:8]) + (" ..." if len(lettering.words) > 8 else "")
        notes.append(
            f"Lettering: the start picture reads {len(lettering.words)} printed word{'s' if len(lettering.words) > 1 else ''} "
            f"({shown}); the prompt sent holds them as they are and the negative prompt refuses new lettering."
        )
    if lettering.invented:
        notes.append(
            f"The start picture's unreadable lettering ({len(lettering.invented)} place{'s' if len(lettering.invented) > 1 else ''}) "
            "is covered by keep_regions: drawn back from the picture in every frame."
        )
    return lettering.as_dict(), notes


def _sent(params: dict, request: VideoRequest, provider: Any) -> tuple[str, str | None]:
    """What the worker sends *request*'s model (``runners.run_clip``): :func:`provider_prompts` of its art direction."""
    prompt, negative = shot_prompts(
        params,
        request.prompt,
        request.negative_prompt,
        negative_field=takes_negative_prompt(params, provider, request.model),
    )
    return provider_prompts(params, prompt, negative)


def ingredient_path(
    provider: Any, model: str, count: int, *, start_frame: bool, logos: bool = False, tracked: bool = False
) -> str:
    """How *count* ingredients reach *model*: its own reference mechanism when it takes them all, else the prompt.

    Through the prompt, a logo among them (*logos*) is drawn back onto the
    shot's frames from its start frame (:data:`PATH_COMPOSITED`); a
    logo with its subject named (*tracked*) is drawn onto that subject
    wherever the take shows it, start frame or not (:data:`PATH_TRACKED`).
    A model that takes the pictures is sent them first: its take is
    checked for the logo, and drawn onto the subject when it fails.
    """
    fits = 0 < count <= int(provider.max_reference_images(model))
    if fits and (not start_frame or provider.reference_images_with_start_frame(model)):
        return PATH_NATIVE
    if tracked:
        return PATH_TRACKED
    return PATH_COMPOSITED if logos and start_frame else PATH_PROMPT


def _path_of(provider: Any, model: str, ingredients: list[Ingredient], *, start_frame: bool) -> str:
    return ingredient_path(
        provider,
        model,
        len(ingredients),
        start_frame=start_frame,
        logos=any(item.role == "logo" for item in ingredients),
        tracked=any(item.tracked for item in ingredients),
    )


async def resolve_ingredients(
    db: Any,
    user: Any,
    raw: Any,
    *,
    provider: Any,
    model: str,
    start_frame: bool,
    fit: str | None = None,
    notes: list[str] | None = None,
) -> tuple[list[Ingredient], str | None]:
    """A shot's ingredients and how they reach its model, checked before anything is paid for, or :class:`ClipError`.

    Each is a picture the person can read and a provider can read
    (:func:`check_start_picture`), a logo's subject's picture too.
    They go through the model's own mechanism when it takes them - with the
    shot's start frame where there is one (:func:`ingredient_path`);
    otherwise they are named in the prompt (:func:`ingredient_prompt`), which
    points at the shot's start frame: a shot with neither has nothing to keep
    them from and is refused by the model's name - unless every ingredient is
    a logo drawn onto its subject, which the take itself shows. Sent to the
    model as pictures, each is of a shape it takes (:func:`check_reference_shape`):
    refused before anything is paid, or padded with *fit*.
    """
    from app.core.utils import safe_uuid
    from app.models.file_attachment import FileAttachment
    from app.services.file_access import can_read_file

    ingredients = parse_ingredients(raw)
    if not ingredients:
        return [], None

    pictures: dict[str, tuple[Any, StartFrame | None]] = {}

    async def readable(file_id: str, what: str) -> str:
        uid = safe_uuid(file_id)
        row = await db.get(FileAttachment, uid) if uid else None
        if row is None or not (row.content_type or "").startswith("image/") or not await can_read_file(row, user, db):
            raise ClipError(f"No picture `{file_id}` you can read to use as {what}.")
        pictures[str(row.id)] = (row, await check_start_picture(row))
        return str(row.id)

    checked: list[Ingredient] = []
    for item in ingredients:
        file_id = await readable(item.file_id, "an ingredient")
        subject = item.subject
        if subject is not None:
            subject = Subject(await readable(subject.file_id, f"the subject of {item.name}"), subject.label)
        checked.append(Ingredient(file_id, item.role, item.label, item.quad, subject))
    path = _path_of(provider, model, checked, start_frame=start_frame)
    if path == PATH_NATIVE:
        # Sent to the model as its reference images: of a shape it takes, before anything is paid.
        for item in checked:
            row, picture = pictures[item.file_id]
            await check_reference_shape(row, picture, provider=provider, model=model, fit=fit, notes=notes)
    if needs_start_frame(checked, path) and not start_frame:
        raise ClipError(
            f"{model} takes no reference images for this shot, and a shot without a start frame has no picture for "
            "its prompt to point at: start it from a picture showing the ingredients (compose_picture, then "
            "reference_file_id), pick a model with reference_images in video_studio_capabilities, or give a logo its "
            "subject (ingredients[].subject) to have it drawn onto the subject wherever the take shows it",
            about_model=True,
        )
    return checked, path


def keep_ingredients(params: dict, ingredients: list[Ingredient], path: str | None) -> None:
    """Record a shot's ingredients and their path in *params*; native ones are its reference images too."""
    if not ingredients:
        for key in (INGREDIENTS, INGREDIENTS_PATH):
            params.pop(key, None)
        return
    params[INGREDIENTS] = [item.as_dict() for item in ingredients]
    params[INGREDIENTS_PATH] = path
    if path == PATH_NATIVE:
        params[REFERENCE_IMAGES] = [item.file_id for item in ingredients]
    else:
        params.pop(REFERENCE_IMAGES, None)


# ── Consistency with the video's references ──────────────────────

#: The params key of the video's references a shot is held to (``reference_drift.CONSISTENCY``): ``{video_id,
#: references: [{id, file_id, label, kind, description}], path: native|prompt}``. The worker checks the take against
#: them before it is accepted, the save's quality check reads the kept frames against them (``reference_drift``).
CONSISTENCY = "consistency"
#: The most references one shot is held to: what a model takes as reference images, at most.
MAX_CONSISTENCY = MAX_REFERENCE_IMAGES


def _consistency_ids(raw: Any) -> list[str]:
    """The reference ids a call names in ``consistency`` (a list, or one id), once each, or :class:`ClipError`."""
    if raw in (None, "", []):
        return []
    items = raw if isinstance(raw, list | tuple) else [raw]
    if not all(isinstance(item, str) and item.strip() for item in items):
        raise ClipError("consistency lists the ids of the video's references (video_id) the shot must match.")
    ids = list(dict.fromkeys(item.strip() for item in items))
    if len(ids) > MAX_CONSISTENCY:
        raise ClipError(f"A shot is held to at most {MAX_CONSISTENCY} of the video's references (consistency).")
    return ids


async def resolve_consistency(
    db: Any, user: Any, raw: Any, *, video_id: object, provider: Any, model: str, start_frame: bool, reference_images: int
) -> dict | None:
    """The video's references a shot is held to (``consistency``), and how they reach *model*, or :class:`ClipError`.

    Each id names a reference of the video *video_id* (``spec.references``);
    their pictures are sent as the model's reference images when it takes
    them all - beside the shot's start frame where there is one, and beside
    the reference images the call names - (:data:`PATH_NATIVE`), else they
    are named in the prompt from the reference's label and description
    (:data:`PATH_PROMPT`, :func:`consistency_prompt`). ``None`` for none.
    """
    from app.services.video_studio.projects import load_visible_project
    from app.services.video_studio.spec import references_of

    ids = _consistency_ids(raw)
    if not ids:
        return None
    if not video_id:
        raise ClipError("consistency holds the shot to a video's references: name the video (video_id).")
    project = await load_visible_project(db, video_id, user)
    known = references_of(project.spec or {}) if project is not None else {}
    unknown = [item for item in ids if item not in known]
    if unknown:
        listed = ", ".join(sorted(known)) or "none"
        raise ClipError(
            f"consistency names no reference of the video: {', '.join(repr(item) for item in unknown)} (the video's "
            f"references: {listed}) - add it to the video's references (edit_video) first."
        )
    references = [dict(known[item]) for item in ids]
    count = reference_images + len(references)
    fits = count <= int(provider.max_reference_images(model))
    native = fits and (not start_frame or provider.reference_images_with_start_frame(model))
    return {"video_id": str(video_id), "references": references, "path": PATH_NATIVE if native else PATH_PROMPT}


def consistency_of(params: dict) -> list[dict]:
    """The video's references a shot's params hold it to, ``[{id, file_id, label, kind, description}]``."""
    kept = params.get(CONSISTENCY)
    if not isinstance(kept, dict):
        return []
    return [dict(item) for item in kept.get("references") or [] if isinstance(item, dict) and item.get("file_id")]


def keep_consistency(params: dict, consistency: dict | None) -> None:
    """Record the references a shot is held to in *params*; on the native path they are its reference images too."""
    if not consistency:
        params.pop(CONSISTENCY, None)
        return
    params[CONSISTENCY] = consistency
    if consistency.get("path") == PATH_NATIVE:
        own = [fid for fid in params.get(REFERENCE_IMAGES) or []]
        params[REFERENCE_IMAGES] = list(dict.fromkeys([*own, *(str(item["file_id"]) for item in consistency["references"])]))


def consistency_prompt(prompt: str, params: dict) -> str:
    """*prompt* with each reference the shot is held to named, for a model that takes no picture of it beside the
    start frame: ``Keep the <label> exactly as in the start picture: <description>.`` (without a start frame, the
    description alone holds it)."""
    kept = params.get(CONSISTENCY)
    if not isinstance(kept, dict) or kept.get("path") != PATH_PROMPT:
        return prompt
    where = " exactly as in the start picture" if starts_from_picture(params) else ""
    lines = []
    for item in consistency_of(params):
        label = str(item.get("label") or item.get("id"))
        described = f": {item['description']}" if item.get("description") else ""
        lines.append(f"Keep {label}{where}{described}.")
    return f"{prompt.rstrip()} {' '.join(lines)}".strip() if lines else prompt


def consistency_note(params: dict) -> str | None:
    """How the shot is held to the video's references, as the answer says it; ``None`` for none."""
    kept = params.get(CONSISTENCY)
    if not isinstance(kept, dict):
        return None
    names = ", ".join(f"`{item.get('id')}`" for item in consistency_of(params))
    if kept.get("path") == PATH_NATIVE:
        return f"Held to the video's references {names}: their pictures are sent as the model's reference images; the take is checked against them."
    return (
        f"Held to the video's references {names}, named in the prompt (the model takes no reference image beside a "
        "start frame): the take is checked against them."
    )


@dataclass(frozen=True)
class StartFrame:
    """The first frame a shot is asked with, and the stored file it is (the start image, the frame kept)."""

    data: bytes
    mime: str
    file_id: str | None = None
    #: Cut from its video and saved by this call: ``params`` name it now and are to be stored.
    saved: bool = False
    #: Drawn from an SVG by the platform's rasteriser: the answer says so.
    svg: bool = False


async def lint_start_shown(user: Any, params: dict) -> list[str]:
    """Read a shot's start picture against its negative prompt before anything is paid (issue #10053): the terms it
    already shows are kept in the params (``content_drift.START_SHOWN_KEY``) for the take's content check, which never
    fails the first frame on them, and said in the notes ``validate_only`` answers. A shot from no picture, with no
    negative prompt or no content check, or a picture not stored yet (a video frame cut at generation), reads none.
    """
    import tempfile

    from app.services.video_studio import assets, content_drift

    file_id = start_frame_file_id(params)
    negative = params.get("negative_prompt")
    digest = content_drift.start_digest(negative, file_id)
    if digest is None or params.get("content_check", True) is False:
        params.pop(content_drift.START_SHOWN_KEY, None)
        return []
    kept = content_drift.kept_start_shown(params, digest)
    if kept is None:
        try:
            with tempfile.TemporaryDirectory() as workdir:
                path = Path(workdir) / "start-picture"
                row = await assets.download_file(file_id, str(path))
                if row is None:
                    return []
                picture = await start_picture(path.read_bytes(), row.content_type)
        except Exception as exc:  # advisory: the take's check then reads the picture it starts from
            logger.info("Start picture not read against the negative prompt: %s", format_exception_message(exc))
            return []
        kept = await content_drift.read_start_picture(getattr(user, "id", None), (picture.data, picture.mime), negative)
        content_drift.keep_start_shown(params, digest, kept)
    note = content_drift.start_shown_note(kept or [])
    return [note] if note else []


async def lint_start_content(user: Any, params: dict, prompt: str | None) -> None:
    """Read a shot's start picture against the subjects its prompt names before anything is paid (issue #10895): a
    first-frame finding the picture itself shows (look-alike subjects asked distinct, a subject of another kind,
    deformed or replaced) would fail every take's first frame the same way, so the request is refused with
    ``content_drift.START_FAILS_CODE``; the reading is kept in the params (``content_drift.START_FINDINGS_KEY``) so a
    take's failure it shares is said inherited from the start picture. A shot from no picture, with no prompt or no
    content check, a picture not stored yet or one that cannot be read, is never refused.
    """
    import tempfile

    from app.services.video_studio import assets, content_drift

    file_id = start_frame_file_id(params)
    digest = content_drift.start_content_digest(prompt, params.get("negative_prompt"), file_id)
    if digest is None or params.get("content_check", True) is False:
        params.pop(content_drift.START_FINDINGS_KEY, None)
        return
    kept = content_drift.kept_start_findings(params, digest)
    if kept is None:
        try:
            with tempfile.TemporaryDirectory() as workdir:
                path = Path(workdir) / "start-picture"
                row = await assets.download_file(file_id, str(path))
                if row is None:
                    return
                picture = await start_picture(path.read_bytes(), row.content_type)
        except Exception as exc:  # advisory: the take's check then reads the picture it starts from
            logger.info("Start picture not read against the prompt's subjects: %s", format_exception_message(exc))
            return
        kept = await content_drift.read_start_content(
            getattr(user, "id", None),
            (picture.data, picture.mime),
            prompt=str(prompt),
            negative_prompt=params.get("negative_prompt"),
            params=params,
        )
        content_drift.keep_start_findings(params, digest, kept)
    if kept:
        raise ClipError(content_drift.start_fails_reason(kept), unpaid=True)


def start_frame_file_id(params: dict | None) -> str | None:
    """The file a shot's first frame is: the frame kept from its video, else its start image; ``None`` for none."""
    params = params or {}
    source = params.get("reference_video")
    if isinstance(source, dict):
        return str(source["frame_file_id"]) if source.get("frame_file_id") else None
    return str(params["reference_file_id"]) if params.get("reference_file_id") else None


def _source_label(source: dict) -> str:
    noun = {"recording": "recording", "shot": "AI shot"}.get(source.get("kind"), "video")
    name = source.get("name")
    return f'{noun} "{name}"' if name else noun


def frame_label(source: dict) -> str:
    """Which frame of which video *source* (``params.reference_video``) names: ``the last frame of the AI shot "x"``."""
    at = source.get("at_seconds")
    if source.get("step") is not None and at is not None:
        moment = f"the frame after step {source['step']} ({at:.1f} s)"
    elif at is not None:
        moment = f"the frame at {at:.1f} s"
    else:
        moment = "the last frame"
    cropped = ", its middle cut to the shot's shape" if source.get("crop") else ""
    late = ""
    if source.get("clamped_from") is not None:
        # A moment past its end: the last frame, and why.
        late = f" (asked at {float(source['clamped_from']):.1f} s; it lasts {float(source.get('lasts') or 0):.1f} s)"
    return f"{moment} of the {_source_label(source)}{late}{cropped}"


def start_note(params: dict) -> str | None:
    """Which frame of which video a shot starts from, as its notes say it; ``None`` for an image or none."""
    source = params.get("reference_video")
    if not isinstance(source, dict) or not source.get("file_id"):
        return None
    return f"Started from {frame_label(source)}."


async def _frame_of(
    source: dict,
    workdir: str,
    *,
    max_side: int,
    name: str,
    for_shot: bool = False,
    aspect_ratio: str | None = None,
    clamp: bool = False,
) -> tuple[Path, Any]:
    """The frame of a video source (``params.reference_video``) its ``at_seconds`` names, else its last, as *name*.

    For a shot (*aspect_ratio*), a video of another shape than the shot's is
    refused - its size is read here, before the provider is asked - unless
    the source says ``crop``, which cuts its middle to that ratio before
    scaling. Returns the frame and the video's probe; the video is
    left in *workdir* (``reference-video``) for whoever looks at it further.
    *clamp*: a moment past its end gives its last frame, *source* updated to
    say so (``clamped_from``).
    """
    from app.services.video_studio import assets, encoder

    what = _source_label(source)
    video = Path(workdir) / "reference-video"
    if await assets.download_file(source["file_id"], str(video)) is None:
        if for_shot:
            raise ClipError(f"The {what} this shot starts from is gone: start the shot from another one.")
        raise ClipError(f"The {what} is gone: take the frame from another one.")
    at = source.get("at_seconds")
    frame = Path(workdir) / name
    crop_to = _shape(aspect_ratio) if source.get("crop") else None
    try:
        info = await encoder.probe(str(video))
        duration = info.duration
        if at is not None and duration and at > duration + _END_MARGIN_SECONDS:
            if not clamp:
                moment = "the shot cannot start at" if for_shot else "it has no frame at"
                raise ClipError(f"The {what} lasts {duration:.1f} s: {moment} {at:.1f} s.")
            source.update(_clamped(source, at, duration))
            at = None
        if aspect_ratio:
            _check_shape(f"The {what}", (info.width, info.height), aspect_ratio, source.get("crop"))
        seek = None if at is None or (duration and at >= duration - _END_MARGIN_SECONDS) else at
        await encoder.extract_still(str(video), at=seek, out_path=str(frame), max_side=max_side, crop_to=crop_to)
    except encoder.EncoderError as exc:
        raise ClipError(f"No frame could be taken from the {what}: {exc}") from None
    if not frame.exists() or not frame.stat().st_size:
        raise ClipError(f"No frame could be taken from the {what}.")
    return frame, info


def resolved_prefix_note(given: Any, source: dict) -> str:
    """``"`48b4e177` is AI shot `48b4e177-...`. "`` when *given* was an id prefix that *source* (:func:`resolve_frame`)
    stands for, so the next call names the full id; ``""`` otherwise."""
    from app.services.builtin_tools.id_prefix import id_prefix

    if id_prefix(given) is None or not source.get("id"):
        return ""
    noun = {"recording": "recording", "shot": "AI shot"}.get(source.get("kind"), "file")
    return f"`{str(given).strip()}` is {noun} `{source['id']}`. "


#: The largest side of a frame kept as a picture: a 4K render's.
FRAME_PICTURE_MAX_SIDE = 3840


async def resolve_frame(
    db: Any,
    user: Any,
    *,
    recording_id: Any = None,
    clip_id: Any = None,
    file_id: Any = None,
    at_seconds: Any = None,
    at_step: Any = None,
) -> dict:
    """The video source of a frame kept as a picture, checked like a shot's first frame, or :class:`ClipError`.

    After a recording's step the frame its contact sheet shows, else the one
    at *at_seconds*, else the last (:func:`resolve_reference`); a moment past
    the video's end is its last frame, said so.
    """
    if file_id and not (recording_id or clip_id):
        from app.core.utils import safe_uuid
        from app.models.file_attachment import FileAttachment
        from app.services.builtin_tools._shared import _file_handle_key

        # An id prefix of one of the person's files is that file (resolve_reference resolves it again: no read).
        file_id = await _full_id(
            db, user, FileAttachment, _file_handle_key(str(file_id)), noun="files", label=FileAttachment.filename
        )
        uid = safe_uuid(str(file_id))
        row = await db.get(FileAttachment, uid) if uid else None
        if row is not None and (row.content_type or "").startswith("image/"):
            raise ClipError("That file is already a picture: use its file_id as it is.")
    reference = await resolve_reference(
        db,
        user,
        file_id=file_id,
        recording_id=recording_id,
        clip_id=clip_id,
        at_seconds=at_seconds,
        at_step=at_step,
        clamp=True,
    )
    source = reference.get("reference_video")
    if not source:
        raise ClipError("Name the recording, the AI shot or the video file to take a frame of.")
    return source


async def frame_picture(source: dict, workdir: str, *, max_side: int = FRAME_PICTURE_MAX_SIDE) -> bytes:
    """The PNG of the frame *source* names (:func:`resolve_frame`), at up to *max_side* (:data:`FRAME_PICTURE_MAX_SIDE`).

    Large enough for an image scene of a 4K render, a statement backdrop or
    a shot's start picture. A moment past the video's end is its last frame,
    *source* updated to say so. Raises :class:`ClipError`.
    """
    frame, _info = await _frame_of(source, workdir, max_side=max_side, name="frame.png", clamp=True)
    return frame.read_bytes()


#: A region of a frame is cut from the frame at its own size: never scaled, up to this side.
REGION_FRAME_MAX_SIDE = 8192


def frame_region(raw: Any) -> tuple[float, float, float, float]:
    """The box ``(x0, y0, x1, y1)``, 0-1 of the frame, a ``save_video_frame`` *region* names, or :class:`ClipError`.

    ``{"box": [x0, y0, x1, y1]}``, or ``{"quad": [[x, y] x 4]}`` - a screen's
    corners - cut by its bounding box: a crop at 100 % is the frame's own
    pixels, never warped.
    """
    if not isinstance(raw, dict) or bool(raw.get("box") is not None) == bool(raw.get("quad") is not None):
        raise ClipError("region is {box: [x0, y0, x1, y1]} or {quad: [[x, y] x 4]}, 0-1 across and down the frame.")
    try:
        if raw.get("box") is not None:
            x0, y0, x1, y1 = (float(v) for v in raw["box"])
        else:
            points = [(float(x), float(y)) for x, y in raw["quad"]]
            if len(points) != 4:
                raise ValueError
            xs, ys = [p[0] for p in points], [p[1] for p in points]
            x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
    except (TypeError, ValueError):
        raise ClipError("region is {box: [x0, y0, x1, y1]} or {quad: [[x, y] x 4]}, 0-1 across and down the frame.") from None
    x0, y0, x1, y1 = (min(1.0, max(0.0, v)) for v in (x0, y0, x1, y1))
    if not all(math.isfinite(v) for v in (x0, y0, x1, y1)) or x1 <= x0 or y1 <= y0:
        raise ClipError("region encloses nothing of the frame: x0 < x1 and y0 < y1, 0-1 across and down it.")
    return x0, y0, x1, y1


def cut_region(png: bytes, box: tuple[float, float, float, float]) -> tuple[bytes, tuple[int, int, int, int], tuple[int, int]]:
    """*box* (0-1, :func:`frame_region`) of the frame *png*, cut at 100 %: its PNG, the pixels cut, the frame's size."""
    import io

    from PIL import Image

    picture = Image.open(io.BytesIO(png))
    picture.load()
    width, height = picture.size
    x0, y0, x1, y1 = box
    left, top = math.floor(x0 * width), math.floor(y0 * height)
    right, bottom = max(left + 1, math.ceil(x1 * width)), max(top + 1, math.ceil(y1 * height))
    out = io.BytesIO()
    picture.crop((left, top, min(right, width), min(bottom, height))).save(out, format="PNG")
    return out.getvalue(), (left, top, min(right, width), min(bottom, height)), (width, height)


#: The most frames one ``save_video_frames`` call saves: a minute of video at a frame a second.
SHEET_FRAMES_MOST = 60
#: The frames of one sweep cut at once (each an ffmpeg seek of its own).
_SHEET_CONCURRENCY = 4


def sheet_moments(
    duration: float,
    *,
    at_seconds: Any = None,
    step_seconds: Any = None,
    from_seconds: Any = None,
    to_seconds: Any = None,
) -> list[float]:
    """The moments a frame sweep of a video lasting *duration* saves, in order, or :class:`ClipError`.

    Either the moments *at_seconds* (a list), or every *step_seconds* from *from_seconds* (``0``) up to, not
    including, *to_seconds* (its end): an 8 s insert stepped by 1 s is 8 frames. A moment past the end is its last
    frame. At most :data:`SHEET_FRAMES_MOST`.
    """
    listed = at_seconds if isinstance(at_seconds, (list, tuple)) else None if at_seconds in (None, "") else [at_seconds]
    step = _number(step_seconds, "step_seconds")
    if (listed is None) == (step is None):
        raise ClipError("Give at_seconds (a list of moments) or step_seconds (one frame every so many seconds), one of them.")
    end_of = max(0.0, float(duration or 0.0))
    if listed is not None:
        if from_seconds not in (None, "") or to_seconds not in (None, ""):
            raise ClipError("from_seconds and to_seconds bound a step_seconds sweep; at_seconds lists its moments itself.")
        moments = sorted({_number(value, "at_seconds") or 0.0 for value in listed})
    else:
        start = _number(from_seconds, "from_seconds") or 0.0
        stop = _number(to_seconds, "to_seconds")
        stop = end_of if stop is None else min(stop, end_of) if end_of else stop
        if step is None or step <= 0:
            raise ClipError("step_seconds is a number of seconds above 0.")
        if stop <= start:
            raise ClipError(f"Nothing to sweep: from_seconds {start:.2f} is not before {stop:.2f} s (the video lasts {end_of:.2f} s).")
        count = math.ceil((stop - start) / step - 1e-9)
        if count > SHEET_FRAMES_MOST:
            raise ClipError(
                f"That is {count} frames: at most {SHEET_FRAMES_MOST} a call - a larger step_seconds or a shorter range "
                "(from_seconds, to_seconds)."
            )
        moments = [round(start + i * step, 3) for i in range(count)]
    if not moments:
        raise ClipError("Give at least one moment in at_seconds.")
    if len(moments) > SHEET_FRAMES_MOST:
        raise ClipError(f"That is {len(moments)} moments: at most {SHEET_FRAMES_MOST} a call.")
    if any(t < 0 for t in moments):
        raise ClipError("A moment is 0 seconds or later.")
    return moments


@dataclass
class SweepFrame:
    """One frame of a sweep: its moment, the moment asked when that was past the end, its PNG."""

    at_seconds: float
    png: bytes
    asked: float | None = None


@dataclass
class FrameSweep:
    """The frames of one video a sweep saved, the size they were cut from and the region, if any."""

    frames: list[SweepFrame]
    duration: float
    frame_size: tuple[int, int]
    cut: tuple[int, int, int, int] | None = None


async def frame_sweep(
    source: dict,
    workdir: str,
    *,
    box: tuple[float, float, float, float] | None = None,
    at_seconds: Any = None,
    step_seconds: Any = None,
    from_seconds: Any = None,
    to_seconds: Any = None,
) -> FrameSweep:
    """Many frames of the video *source* names (:func:`resolve_frame`), from ONE download of it.

    The moments are :func:`sheet_moments`'s; each frame is the video's own pixels (a 4K render's at 3840 px), and
    with *box* (:func:`frame_region`) only that region, cut at 100 % like ``save_video_frame``. The generic sweep a
    contact sheet is drawn from (:func:`frame_grid`), whatever the video and whatever the reason to look at it.
    Raises :class:`ClipError`.
    """
    import asyncio

    from app.services.video_studio import assets, encoder

    what = _source_label(source)
    video = Path(workdir) / "sweep-video"
    if await assets.download_file(source["file_id"], str(video)) is None:
        raise ClipError(f"The {what} is gone: take the frames from another one.")
    try:
        info = await encoder.probe(str(video))
    except encoder.EncoderError as exc:
        raise ClipError(f"The {what} could not be read: {exc}") from None
    duration = float(info.duration or 0.0)
    moments = sheet_moments(
        duration, at_seconds=at_seconds, step_seconds=step_seconds, from_seconds=from_seconds, to_seconds=to_seconds
    )
    max_side = REGION_FRAME_MAX_SIDE if box is not None else FRAME_PICTURE_MAX_SIDE
    gate = asyncio.Semaphore(_SHEET_CONCURRENCY)

    async def one(index: int, t: float) -> SweepFrame:
        late = bool(duration) and t > duration + _END_MARGIN_SECONDS
        seek = None if late or (duration and t >= duration - _END_MARGIN_SECONDS) else t
        out = Path(workdir) / f"sweep-{index:03d}.png"
        async with gate:
            try:
                await encoder.extract_still(str(video), at=seek, out_path=str(out), max_side=max_side)
            except encoder.EncoderError as exc:
                raise ClipError(f"No frame could be taken from the {what} at {t:.2f} s: {exc}") from None
        if not out.exists() or not out.stat().st_size:
            raise ClipError(f"No frame could be taken from the {what} at {t:.2f} s.")
        return SweepFrame(at_seconds=min(t, duration) if late else t, png=out.read_bytes(), asked=t if late else None)

    frames = list(await asyncio.gather(*(one(i, t) for i, t in enumerate(moments))))
    size = (int(info.width or 0), int(info.height or 0))
    cut = None
    if box is not None:
        for frame in frames:
            frame.png, cut, size = await asyncio.to_thread(cut_region, frame.png, box)
    return FrameSweep(frames=frames, duration=duration, frame_size=size, cut=cut)


#: A contact sheet's tile width, by how many tiles it holds: large enough to judge, small enough to read whole.
_GRID_TILE_WIDTHS = ((16, 480), (36, 360), (SHEET_FRAMES_MOST, 300))
_GRID_LABEL_HEIGHT = 26
_GRID_GAP = 6


def frame_grid(tiles: Sequence[tuple[str, bytes]]) -> bytes:
    """One PNG of *tiles* (``(label, png)``) in a grid, each labelled under its picture.

    The contact sheet of a frame sweep (each label its moment, ``render_checks.clock``), or of anything that is a
    series of pictures of one size; every tile is scaled to the same width, never cropped, so a region keeps its
    whole box. ``insert_sweep.draw`` is the insert gate's own sheet (100 % crops, failing tiles framed red).
    """
    import io

    from PIL import Image, ImageDraw, ImageFont

    if not tiles:
        raise ClipError("No frame to lay out.")
    count = len(tiles)
    tile_width = next(width for most, width in _GRID_TILE_WIDTHS if count <= most) if count <= SHEET_FRAMES_MOST else 240
    columns = min(count, 4 if count <= 16 else 6 if count <= 36 else 8)
    rows = math.ceil(count / columns)
    pictures = []
    for _label, png in tiles:
        with Image.open(io.BytesIO(png)) as image:
            picture = image.convert("RGB")
        scale = tile_width / max(1, picture.width)
        pictures.append(picture.resize((tile_width, max(1, round(picture.height * scale))), Image.Resampling.LANCZOS))
    tile_height = max(p.height for p in pictures)
    cell_height = tile_height + _GRID_LABEL_HEIGHT
    sheet = Image.new(
        "RGB",
        (columns * tile_width + (columns + 1) * _GRID_GAP, rows * cell_height + (rows + 1) * _GRID_GAP),
        (6, 7, 12),
    )
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.load_default(size=18)
    except TypeError:  # Pillow < 10.1
        font = ImageFont.load_default()
    for i, ((label, _png), picture) in enumerate(zip(tiles, pictures, strict=True)):
        row, column = divmod(i, columns)
        x = _GRID_GAP + column * (tile_width + _GRID_GAP)
        y = _GRID_GAP + row * (cell_height + _GRID_GAP)
        sheet.paste(picture, (x, y))
        draw.text((x + 6, y + tile_height + 4), str(label)[:40], fill=(230, 233, 240), font=font)
    out = io.BytesIO()
    sheet.save(out, format="PNG", optimize=True)
    return out.getvalue()


def _frame_filename(source: dict) -> str:
    stem = re.sub(r"[^A-Za-z0-9]+", "-", str(source.get("name") or "")).strip("-").lower()[:60]
    at = source.get("at_seconds")
    return f"{stem or source.get('kind') or 'video'}-start-frame-{'end' if at is None else f'{at:.1f}s'}.jpg"


def image_size(path: Path) -> tuple[int, int] | None:
    """The pixel size of the picture at *path*; ``None`` when it is not one."""
    from PIL import Image

    try:
        with Image.open(path) as picture:
            return picture.size
    except OSError:  # Pillow's UnidentifiedImageError included
        return None


def _has_shape(frame: Path, aspect_ratio: str) -> bool:
    """Whether a kept frame still fits a shot of *aspect_ratio* (one generated again in another may not)."""
    size, shape = image_size(frame), _shape(aspect_ratio)
    return size is not None and (shape is None or fits(size, shape))


async def save_frame(frame: Path, source: dict, *, user_id: object, conversation_id: object) -> Any:
    """Store a frame cut from *source* as a file of *user_id*'s conversation (``clip-start-frame``).

    It says which frame it is: a second read in a turn answers with that.
    """
    from app.services.video_studio import assets

    size = image_size(frame)
    label = frame_label(source) + (f", {size[0]}x{size[1]}" if size else "")
    return await assets.store_path(
        str(frame),
        filename=_frame_filename(source),
        user_id=user_id,
        source=assets.SOURCE_CLIP_START_FRAME,
        content_type="image/jpeg",
        conversation_id=conversation_id,
        description=f"{label[:1].upper()}{label[1:]}: the frame an AI shot starts from.",
    )


async def start_frame(
    params: dict, workdir: str, *, user_id: object = None, conversation_id: object = None
) -> StartFrame | None:
    """The first frame a shot's params name (:func:`resolve_reference`), ``None`` for none.

    Read on every new generation. A start image goes as a picture every
    provider reads (:func:`start_picture`). A frame cut from a video is saved
    as a file of the shot's conversation and named in
    ``reference_video.frame_file_id`` (``StartFrame.saved``: the caller stores
    the params), so the person sees what the shot opened on and a shot
    generated again starts from that very file; only a frame deleted since,
    or of another shape than the shot is now asked in, is cut again. A source
    gone since fails the shot instead of making another one from the prompt
    alone.
    """
    from app.services.video_studio import assets

    if image := params.get("reference_file_id"):
        path = Path(workdir) / "reference"
        row = await assets.download_file(image, str(path))
        if row is None:
            raise ClipError("The image this shot starts from is gone: start the shot from another one.")
        picture = await start_picture(path.read_bytes(), row.content_type)
        if isinstance(screen := params.get(SCREEN), dict):
            import asyncio

            from app.services.video_studio import screens

            # The model is given the room only: the screen is the flat key it keeps.
            spec, size = screens.ScreenSpec.from_params(screen), tuple(screen["start_size"])
            return StartFrame(await asyncio.to_thread(screens.placeholder, picture.data, spec, size), "image/png", str(image))
        return StartFrame(picture.data, picture.mime, file_id=str(image))
    source = params.get("reference_video")
    if not isinstance(source, dict) or not source.get("file_id"):
        return None
    aspect_ratio = params.get("aspect_ratio") or "16:9"
    kept = source.get("frame_file_id")
    frame = Path(workdir) / "start-frame.jpg"
    if kept and await assets.download_file(kept, str(frame)) is not None and _has_shape(frame, aspect_ratio):
        return StartFrame(frame.read_bytes(), "image/jpeg", file_id=str(kept))
    frame, _info = await _frame_of(
        source,
        workdir,
        max_side=START_FRAME_MAX_SIDE,
        name="start-frame.jpg",
        for_shot=True,
        aspect_ratio=aspect_ratio,
    )
    try:
        row = await save_frame(frame, source, user_id=user_id, conversation_id=conversation_id)
    except Exception as exc:  # the frame is in hand: a storage hiccup never fails the shot
        logger.warning("The start frame of a shot was not saved: %s", format_exception_message(exc))
        return StartFrame(frame.read_bytes(), "image/jpeg")
    params["reference_video"] = {**source, "frame_file_id": str(row.id)}
    return StartFrame(frame.read_bytes(), "image/jpeg", file_id=str(row.id), saved=True)


# ── Previewing a start frame before paying ────────────────────────

#: A recording's frame is still loading when a frame this soon after it differs from it ...
SETTLE_WINDOW_SECONDS = 1.0
#: ... in more than this share of its pixels (a spinner giving way to the page, a blank page filling in) ...
SETTLE_CHANGE = 0.1
#: ... a pixel differing by more than this many gray levels (under it: the encoder's noise).
SETTLE_NOISE = 24
#: How the frames after it are watched: so many a second, this small, at most this long.
SETTLE_FPS = 10
SETTLE_SIZE = (160, 90)
SETTLE_HORIZON_SECONDS = 6.0
#: A frame is settled only once it is seen unchanged for at least this long.
SETTLE_MIN_SPAN_SECONDS = 0.3


def changed(before: bytes, after: bytes, *, noise: int = SETTLE_NOISE) -> float:
    """The share of the pixels of two gray frames of one size that differ by more than *noise* levels."""
    import numpy as np

    a = np.frombuffer(before, dtype=np.uint8).astype(np.int16)
    b = np.frombuffer(after, dtype=np.uint8).astype(np.int16)
    return float(np.count_nonzero(np.abs(a - b) > noise)) / max(1, a.size)


def settle(
    frames: list[bytes],
    *,
    fps: float = SETTLE_FPS,
    window: float = SETTLE_WINDOW_SECONDS,
    change: float = SETTLE_CHANGE,
    min_span: float = SETTLE_MIN_SPAN_SECONDS,
) -> tuple[bool, int | None]:
    """``(loading, settled)`` of evenly spaced frames, the first being the frame asked.

    A frame is still loading when one of the frames *window* seconds after it
    differs from it by more than *change*; it is settled when none does, over
    at least *min_span* seconds of them. ``settled`` is the index of the first
    settled frame after a loading one, ``None`` when none settles in the frames
    given. A first frame with too few frames after it to tell is not loading.
    """
    reach = max(1, round(window * fps))
    need = max(1, round(min_span * fps))

    def steady(index: int) -> bool | None:
        after = frames[index + 1 : index + 1 + reach]
        if len(after) < need:
            return None
        return all(changed(frames[index], frame) <= change for frame in after)

    if steady(0) is not False:
        return False, None
    for index in range(1, len(frames)):
        verdict = steady(index)
        if verdict is None:
            break
        if verdict:
            return True, index
    return True, None


def _watch_until(events: list[dict], at: float, duration: float) -> float:
    """How far after *at* a recording's frames tell a page loading: until its next step acts on the page.

    A change that step makes is the script's, not the page loading; so is
    nothing past the horizon or the recording's end.
    """
    from app.services.video_studio.spec import PASSIVE_ACTIONS

    acts = [
        float(event.get("ta", event.get("t0")) or 0.0)
        for event in events
        if event.get("action") not in PASSIVE_ACTIONS and event.get("ok", True)
    ]
    return min([at + SETTLE_HORIZON_SECONDS, duration, *(t for t in acts if t > at)])


async def _loading(video: Path, at: float, until: float, workdir: str) -> tuple[bool, float | None]:
    """Whether a recording's page is still loading at *at* and when it settles (``None``: not by *until*)."""
    from app.services.video_studio import encoder

    if until - at < SETTLE_MIN_SPAN_SECONDS:
        return False, None
    try:
        frames = await encoder.gray_frames(
            str(video),
            start=at,
            seconds=until - at,
            fps=SETTLE_FPS,
            size=SETTLE_SIZE,
            out_path=str(Path(workdir) / "settle.gray"),
        )
    except encoder.EncoderError as exc:  # the frame stands without the advice
        logger.warning("The frames after a start frame were not read: %s", format_exception_message(exc))
        return False, None
    loading, settled = settle(frames)
    return loading, None if settled is None else round(at + settled / SETTLE_FPS, 1)


@dataclass(frozen=True)
class PreviewPlan:
    """A start frame :func:`plan_start_frame_preview` checked: what a start records, a recording's action log."""

    reference: dict
    aspect_ratio: str
    events: list


@dataclass(frozen=True)
class FramePreview:
    """The frame a shot would open on (:func:`preview_start_frame`), and what to know of it."""

    file_id: str
    note: str
    at_seconds: float | None = None
    size: tuple[int, int] | None = None
    #: The recording's page was still loading at ``at_seconds`` ...
    loading: bool = False
    #: ... and settled here, ``None`` when not within what was watched.
    settled_at: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "file_id": self.file_id,
            "at_seconds": self.at_seconds,
            "width": self.size[0] if self.size else None,
            "height": self.size[1] if self.size else None,
            "loading": {"at_seconds": self.at_seconds, "settled_at_seconds": self.settled_at} if self.loading else None,
        }


async def plan_start_frame_preview(
    db: Any,
    user: Any,
    *,
    file_id: Any = None,
    recording_id: Any = None,
    clip_id: Any = None,
    at_seconds: Any = None,
    at_step: Any = None,
    aspect_ratio: str = "16:9",
    crop: Any = None,
) -> PreviewPlan:
    """Check a preview's ``reference_*`` arguments as a start does (:func:`resolve_reference`), or :class:`ClipError`.

    The only part that reads the database: the caller lets its session go
    before :func:`preview_start_frame` downloads and decodes the video.
    """
    from app.core.utils import safe_uuid
    from app.models.video_studio import VideoTake

    reference = await resolve_reference(
        db,
        user,
        file_id=file_id,
        recording_id=recording_id,
        clip_id=clip_id,
        at_seconds=at_seconds,
        at_step=at_step,
        aspect_ratio=aspect_ratio,
        crop=crop,
    )
    if not reference:
        raise ClipError("Name the image, recording, shot or video file a shot would start from.")
    events: list = []
    source = reference.get("reference_video") or {}
    if source.get("kind") == "recording":
        take = await db.get(VideoTake, safe_uuid(source["id"]))
        events = list(take.events or []) if take is not None else []
    return PreviewPlan(reference=reference, aspect_ratio=aspect_ratio, events=events)


def _preview_note(
    source: dict, size: tuple[int, int] | None, loading: bool, settled: float | None, until: float
) -> str:
    at = source.get("at_seconds")
    shown = f"This is {frame_label(source)}" + (f", {size[0]}x{size[1]}." if size else ".")
    advice = ""
    if loading and settled is not None:
        instead = " instead of reference_at_step" if source.get("step") is not None else ""
        advice = (
            f" Careful: page still loading at {at:.1f} s, settled at {settled:.1f} s - start from the settled page "
            f"with reference_at_seconds={settled:.1f}{instead}."
        )
    elif loading:
        advice = (
            f" Careful: page still loading at {at:.1f} s and not settled by {until:.1f} s: pick a later moment "
            "(reference_at_seconds) once it has."
        )
    return (
        f"{shown}{advice} Look at it: if it is not what the shot should open on, move reference_at_seconds and "
        "preview again, then call generate_video_clip with the same reference arguments."
    )


async def preview_start_frame(plan: PreviewPlan, *, user_id: object, conversation_id: object = None) -> FramePreview:
    """The frame a shot asked with *plan*'s arguments would open on, before anything is paid for.

    The worker's own frame (``_frame_of``: the moment, the shape, the crop),
    saved as a file of the conversation (``clip-start-frame``) for the agent or
    the person to look at; an image is its own frame. A recording's page
    caught still loading (:func:`settle` on the frames after it, up to its next
    step) is flagged with the moment it settles, to start from instead.
    """
    import tempfile

    if image := plan.reference.get("reference_file_id"):
        return FramePreview(file_id=str(image), note="An image is the frame as it is: a shot starts from it unchanged.")
    source = plan.reference["reference_video"]
    with tempfile.TemporaryDirectory(prefix="xtm-start-frame-") as workdir:
        frame, info = await _frame_of(
            source,
            workdir,
            max_side=START_FRAME_MAX_SIDE,
            name="start-frame.jpg",
            for_shot=True,
            aspect_ratio=plan.aspect_ratio,
        )
        at = source.get("at_seconds")
        loading, settled, until = False, None, info.duration
        if source.get("kind") == "recording" and at is not None:
            until = _watch_until(plan.events, at, info.duration)
            loading, settled = await _loading(Path(workdir) / "reference-video", at, until, workdir)
        size = image_size(frame)
        row = await save_frame(frame, source, user_id=user_id, conversation_id=conversation_id)
    return FramePreview(
        file_id=str(row.id),
        note=_preview_note(source, size, loading, settled, until),
        at_seconds=at if at is not None else round(info.duration, 3),
        size=size,
        loading=loading,
        settled_at=settled,
    )


#: The arguments naming a shot's first frame (``start_clip``, a retry), as :func:`resolve_reference` takes them.
START_FIELDS: dict[str, str] = {
    "reference_file_id": "file_id",
    "reference_recording_id": "recording_id",
    "reference_clip_id": "clip_id",
    "reference_at_seconds": "at_seconds",
    "reference_at_step": "at_step",
    "crop": "crop",
}
#: Those of them that name the image or the video a shot starts from.
START_SOURCES: tuple[str, ...] = ("reference_file_id", "reference_recording_id", "reference_clip_id")
#: The argument naming the video a start frame was cut from, by its ``reference_video.kind``.
_SOURCE_ARGUMENT = {"recording": "reference_recording_id", "shot": "reference_clip_id", "file": "reference_file_id"}
#: The medium a ``reference_video.kind`` is, as ``video_projects.asset_refs`` names it.
_SOURCE_MEDIUM = {"recording": "take", "shot": "clip", "file": "file"}


def source_refs(params: dict | None) -> list[str]:
    """The media a shot is made from, as ``asset_refs`` keys, sorted: what generating it again needs.

    Its start: the picture (``reference_file_id``), or the recording, the shot
    or the video file a frame was cut from and the frame kept
    (``reference_video``); its reference images and its ingredients' pictures.
    Stored on the shot (``VideoClip.source_refs``); a video that plays the
    shot lists them with its own (``retention.media_index``).
    """
    from app.services.video_studio.spec import asset_ref

    params = params or {}
    named: list[tuple[str, Any]] = [("file", params.get("reference_file_id"))]
    video = params.get("reference_video")
    if isinstance(video, dict):
        named += [(_SOURCE_MEDIUM.get(str(video.get("kind")), ""), video.get("id")), ("file", video.get("frame_file_id"))]
    named += [("file", file_id) for file_id in params.get(REFERENCE_IMAGES) or []]
    for item in params.get(INGREDIENTS) or []:
        if isinstance(item, dict):
            # A logo's subject's picture is read again on every generation, like the logo's own.
            subject = item.get("subject") if isinstance(item.get("subject"), dict) else {}
            named += [("file", item.get("file_id")), ("file", subject.get("file_id"))]
    keys = {asset_ref(kind, value) for kind, value in named if kind and value}
    return sorted(key for key in keys if key)


@dataclass
class ClipPlan:
    """A shot as it is to be asked, every check passed (:func:`plan_clip`, :func:`plan_restart`): nothing stored or paid."""

    request: VideoRequest
    config: Any
    params: dict
    #: What asking the shot changed by itself (a start frame dropped), for the answer to say.
    notes: list[str] = field(default_factory=list)
    #: The prompt and negative prompt the model is sent, the art direction applied: the worker's.
    sent: tuple[str, str | None] | None = None
    #: A shot generated again: its ready take ``kept`` playing until the new one is ready, or
    #: ``discarded`` (``discard: true``); ``None`` when it has none.
    previous_take: str | None = None

    @property
    def label(self) -> str:
        return getattr(self.config, "display_name", None) or self.config.provider

    def summary(self, *, cooling: Any = None) -> dict[str, Any]:
        """The resolved request, as ``generate_video_clip(validate_only=true)`` answers it.

        *cooling* is its provider's state (``provider_cooling.Cooling``): what a shot queued now meets.
        """
        from app.services.video_studio import kept_regions, stray_screens

        params = self.params
        source = params.get("reference_video")
        if isinstance(source, dict):
            start = {
                "kind": source.get("kind"),
                "id": source.get("id"),
                "frame": frame_label(source),
                "frame_file_id": source.get("frame_file_id"),
            }
        elif params.get("reference_file_id"):
            start = {"kind": "image", "file_id": str(params["reference_file_id"])}
        else:
            start = None
        direction = params.get(DIRECTION) if isinstance(params.get(DIRECTION), dict) else {}
        sent_prompt, sent_negative = self.sent or (self.request.prompt, self.request.negative_prompt)
        return {
            "model": self.request.model,
            "provider": self.label,
            "provider_id": params.get("provider_id"),
            "prompt": self.request.prompt,
            "duration_seconds": self.request.duration_seconds,
            "aspect_ratio": self.request.aspect_ratio,
            "resolution": self.request.resolution,
            "negative_prompt": self.request.negative_prompt,
            "sound": self.request.sound,
            "mode": request_mode(params),
            "start_frame": start,
            "reference_image_file_ids": list(params.get(REFERENCE_IMAGES) or []),
            "ingredients": [item.as_dict() for item in ingredients_of(params)],
            "ingredients_path": params.get(INGREDIENTS_PATH),
            "min_reference_similarity": params.get(MIN_REFERENCE_SIMILARITY),
            "consistency": params.get(CONSISTENCY),
            "max_camera_motion": params.get("max_camera_motion"),
            "keep_regions": kept_regions.boxes_of(params) or None,
            # The start picture's text regions the take's text check holds it to (#10919): blank or keep them first.
            "text_regions": lettering_regions(params),
            "stray_screens": (params.get(stray_screens.KEY) or {}).get("mode", "fail"),
            # True / false as asked, null when the camera asked still decides it.
            "stabilise": params.get(STABILISE),
            "fallback_models": [
                item.get("model") for item in params.get(FALLBACK_MODELS) or [] if isinstance(item, dict)
            ],
            "art_direction": direction.get("template"),
            "sent_prompt": sent_prompt,
            "sent_negative_prompt": sent_negative,
            "screen": (
                {
                    "file_id": params[SCREEN]["file_id"],
                    "key": (params[SCREEN].get("key") or {}).get("name"),
                    "quad": params[SCREEN].get("quad"),
                    "found": bool(params[SCREEN].get("found")),
                }
                if isinstance(params.get(SCREEN), dict)
                else None
            ),
            # The screen its start picture shows: the bezel a screen insert or a screenshot lands on, flat.
            "surface": params.get(SURFACE) if isinstance(params.get(SURFACE), dict) else None,
            # True / false as asked, null when a screen replacement or a scene's screen insert decides it.
            "insert": params.get(INSERT),
            "previous_take": self.previous_take,
            # The negative prompt's terms its start picture already shows: judged from the middle frame on (#10053).
            "start_shows_excluded": list((params.get("start_shows_excluded") or {}).get("terms") or []),
            "provider_state": cooling.as_dict() if cooling is not None else None,
            "notes": list(self.notes),
        }


async def _check_declared(
    db: Any,
    *,
    user_id: object,
    provider_id: object,
    model: str | None,
    references: int,
    start_frame: bool,
    again: bool = False,
    prefer: Sequence[tuple[object, str]] = (),
) -> None:
    """:func:`check_reference_combination` on the model as its provider declares it, before it is resolved.

    No provider asked, no row locked (``router.declared_video_reference_limits``);
    nothing is checked when only the resolution can tell which model it is, or
    when *user_id* may not use the provider (the resolution refuses).
    A model several providers offer is read where :func:`resolve` asks it (*prefer*).
    """
    from app.llm.router import declared_video_reference_limits

    declared = await declared_video_reference_limits(db, provider_id=provider_id, model=model, user_id=user_id, prefer=prefer)
    if declared is not None:
        chosen, limits = declared
        check_reference_combination(limits, chosen, references=references, start_frame=start_frame, again=again)


async def plan_clip(
    db: Any,
    user: Any,
    *,
    prompt: str,
    model: str | None = None,
    provider_id: object = None,
    duration_seconds: int = 8,
    aspect_ratio: str = "16:9",
    resolution: str = "1080p",
    negative_prompt: str | None = None,
    reference_file_id: str | None = None,
    reference_recording_id: str | None = None,
    reference_clip_id: str | None = None,
    reference_at_seconds: float | None = None,
    reference_at_step: int | None = None,
    crop: str | None = None,
    sound: bool | None = None,
    text_check: bool = True,
    reference_image_file_ids: Any = None,
    content_check: bool = True,
    ingredients: Any = None,
    min_reference_similarity: Any = None,
    video_id: object = None,
    template_id: object = None,
    freestyle: bool = False,
    accept_minor_drift: bool = True,
    screen: Any = None,
    max_camera_motion: Any = None,
    keep_regions: Any = None,
    stray_screens: Any = None,
    stabilise: Any = None,
    fallback_models: Any = None,
    insert: Any = None,
    consistency: Any = None,
    ingredients_quality: str | None = None,
    reference_fit: str | None = None,
) -> ClipPlan:
    """A new shot as :func:`start_clip` asks it, every check passed, or :class:`ClipError`; nothing stored or paid.

    Resolves the model, checks the reference images (:func:`resolve_reference_images`)
    or the ingredients (:func:`resolve_ingredients`) and the
    first frame (:func:`resolve_reference`, of the shot's shape unless
    *crop*), and gives the params the shot is recorded with - the art
    direction of the video it is made for among them (:func:`resolve_direction`,
    *video_id*, *template_id*, *freestyle*). A start frame
    asked with reference images is checked against what the model declares
    before the model is resolved. *sound* ``False`` asks a silent
    shot: the provider is asked for none, and the worker drops any
    track it still makes. *text_check* ``False`` skips the text check of a
    shot made from a picture (``text_drift``): one meant to change its
    words - and the reading of its start picture's lettering before it is
    paid (:func:`lint_lettering`: gibberish in the plate refused, its words
    held in the prompt sent); *content_check* ``False`` skips the check of its subjects against
    the prompt (``content_drift``). *min_reference_similarity* fails
    a take whose first frame drifted from its start frame (``frame_qc``).
    *accept_minor_drift* ``False`` fails the shot on a detail seen only up
    close too. *screen* makes it a screen replacement
    (:func:`resolve_screen`, ``screens.py``): the model draws the room
    around a flat screen, the real screenshot is drawn into it after.
    *max_camera_motion* fails a take whose last frame changed more than that
    from its first (``frame_qc.camera_motion``). *keep_regions* draws
    those boxes of the start picture back into every frame (``kept_regions``);
    *stray_screens* cleans the screens the shot shows against its
    negative prompt instead of failing it (``stray_screens``).
    *stabilise* locks a drifting camera off once the take is made
    (``stabilise``; unset: when the camera is asked still),
    *fallback_models* are the models the shot is generated again with, in
    order, when a take fails its checks (:func:`resolve_fallbacks`), and
    *insert* says whether the shot is footage a screen insert is drawn on
    (:data:`INSERT`): its start picture's screen is measured only then.
    *consistency* names the references of the video (*video_id*) the shot
    must match (:func:`resolve_consistency`): sent as reference
    images where the model takes them, named in the prompt otherwise, and
    the take checked against them. *reference_fit*: a reference image
    or an ingredient sent to the model outside the shape it takes is refused
    (``refuse``, unset) or padded to it (``pad``).
    """
    from app.services.video_studio import frame_qc, insert_keep_out, kept_regions, text_drift
    from app.services.video_studio import stray_screens as stray
    from app.services.video_studio.content_drift import ACCEPT_MINOR_KEY

    fit = reference_fit_choice(reference_fit)
    if not (prompt or "").strip():
        raise ClipError("A shot needs a prompt describing it (subject, action, camera, light, style).")
    insert = insert_choice(insert)
    if insert is False and screen is not None:
        raise ClipError(
            "insert: false says the shot carries no screen insert, and screen draws a screenshot into its screen: "
            "drop one of them."
        )
    if _named(ingredients) and _named(reference_image_file_ids):
        raise ClipError("Give ingredients or reference_image_file_ids, not both: an ingredient is a reference image with a role.")
    ids = _reference_ids(reference_image_file_ids)
    asks_start = any((reference_file_id, reference_recording_id, reference_clip_id))
    # The configured default video model makes the shot; another only when the configuration lists it as a fallback.
    default_model, configured_fallbacks = await configured_models(db)
    model = configured_model(model, default=default_model, fallbacks=configured_fallbacks)
    if default_model is not None and model is None:
        provider_id = None
    # A model several providers offer, named alone: the video's own provider for it first.
    prefer = await video_providers(db, user, video_id, model) if not provider_id else []
    if ids and asks_start:
        await _check_declared(
            db, user_id=user.id, provider_id=provider_id, model=model, references=len(ids), start_frame=True, prefer=prefer
        )
    direction = await resolve_direction(db, user, video_id=video_id, template_id=template_id, freestyle=freestyle)
    # Made for a video that drops the clips' own sound: asked silent, its track dropped at ingest.
    silenced = sound is None and await video_drops_clip_sound(db, user, video_id)
    if silenced:
        sound = False
    tolerance = camera_motion_tolerance(max_camera_motion)
    notes: list[str] = []
    provider, chosen_model, config = await resolve(db, user.id, model=model, provider_id=provider_id, prefer=prefer, notes=notes)
    # A locked-off camera the model does not hold is said before the take is paid for, never asked of another.
    notes.extend(await locked_camera(db, user, (provider, chosen_model, config), tolerance=tolerance, start_frame=asks_start))
    request = build_request(
        model=chosen_model,
        prompt=prompt,
        aspect_ratio=aspect_ratio,
        duration_seconds=int(duration_seconds or 8),
        resolution=resolution,
        negative_prompt=negative_prompt,
        sound=sound,
    )
    await refuse_unpaid(provider, config, request)
    references = await resolve_reference_images(
        db, user, ids, provider=provider, model=request.model, start_frame=asks_start, fit=fit, notes=notes
    )
    reference = await resolve_reference(
        db,
        user,
        file_id=reference_file_id,
        recording_id=reference_recording_id,
        clip_id=reference_clip_id,
        at_seconds=reference_at_seconds,
        at_step=reference_at_step,
        aspect_ratio=request.aspect_ratio,
        crop=crop,
    )
    kept, path = await resolve_ingredients(
        db, user, ingredients, provider=provider, model=request.model, start_frame=bool(reference), fit=fit, notes=notes
    )
    threshold = reference_similarity_threshold(min_reference_similarity, start_frame=bool(reference))
    held = await resolve_consistency(
        db,
        user,
        consistency,
        video_id=video_id,
        provider=provider,
        model=request.model,
        start_frame=bool(reference),
        reference_images=len(references) + (len(kept) if path == PATH_NATIVE else 0),
    )
    stabilised = stabilise_choice(stabilise)
    replaced_screen = (
        await resolve_screen(db, user, screen, reference=reference, aspect_ratio=request.aspect_ratio)
        if screen is not None
        else None
    )
    # The screen the start picture shows, of a shot carrying a screen insert: flat or refused, before anything is
    # paid.
    surface, said = await lint_surface(db, user, reference, replaced_screen, insert=insert)
    notes.extend(said)
    regions = kept_regions_asked(keep_regions, start_frame=bool(reference))
    # The lettering the start picture shows, read before anything is paid: gibberish refused, its words held.
    lettering, said = await lint_lettering(db, user, reference, text_check=text_check, regions=regions)
    notes.extend(said)
    if insert is False:
        cleanup = None
        if stray_screens not in (None, "", "fail"):
            notes.append("stray_screens dropped: insert: false says the shot carries no screen to clean.")
    else:
        cleanup = await resolve_stray_screens(
            db, user, stray_screens, negative_prompt=request.negative_prompt, content_check=content_check
        )
    params = {
        "provider_id": str(config.id),
        "aspect_ratio": request.aspect_ratio,
        "duration_seconds": request.duration_seconds,
        "resolution": request.resolution,
        "negative_prompt": request.negative_prompt,
        **({"sound": request.sound} if request.sound is not None else {}),
        **({SILENT_FOR_VIDEO: True} if silenced else {}),
        **({} if text_check else {"text_check": False}),
        **({} if content_check else {"content_check": False}),
        **({} if accept_minor_drift else {ACCEPT_MINOR_KEY: False}),
        **ingredients_quality_asked(ingredients_quality),
        "reference_file_id": reference.get("reference_file_id"),
        **({"reference_video": reference["reference_video"]} if "reference_video" in reference else {}),
        **({REFERENCE_IMAGES: references} if references else {}),
        **({MIN_REFERENCE_SIMILARITY: threshold} if threshold is not None else {}),
        **({frame_qc.MAX_CAMERA_MOTION: tolerance} if tolerance is not None else {}),
        **({STABILISE: stabilised} if stabilised is not None else {}),
        **({SCREEN: replaced_screen} if replaced_screen else {}),
        **({SURFACE: surface} if surface else {}),
        **({INSERT: insert} if insert is not None else {}),
        **({kept_regions.KEY: {"boxes": regions}} if regions else {}),
        **({stray.KEY: cleanup} if cleanup else {}),
        **({text_drift.START_LETTERING_KEY: lettering} if lettering else {}),
        **({REFERENCE_FIT: fit} if fit == FIT_PAD else {}),
    }
    keep_ingredients(params, kept, path)
    keep_consistency(params, held)
    # What the start picture already shows of the negative prompt: said now, never failed on the first frame.
    notes.extend(await lint_start_shown(user, params))
    # What the start picture already fails of the prompt's subjects: every take would fail on it, refused now.
    await lint_start_content(user, params, prompt)
    fallbacks, said = await resolve_fallbacks(
        db, user, fallback_models, model=request.model, params=params, configured=(default_model, configured_fallbacks)
    )
    if fallbacks:
        params[FALLBACK_MODELS] = fallbacks
    notes.extend(said)
    _record_direction(params, direction)
    if note := sound_note(params):
        notes.append(note)
    if note := consistency_note(params):
        notes.append(note)
    # What a shot carrying a screen insert is asked of its screen: its rectangle kept clear.
    if said := insert_keep_out.note(params, provider, request.model):
        notes.append(said)
    return ClipPlan(request=request, config=config, params=params, notes=notes, sent=_sent(params, request, provider))


async def start_clip(db: Any, user: Any, *, conversation_id: object = None, name: str | None = None, **asked: Any):
    """Queue an AI shot for *user*: ``(clip, provider label)``, flushed and enqueued, not committed.

    The one start of a clip - the agent tool and the editor alike - so both
    check it the same way and record the same params (:func:`plan_clip`,
    whose arguments *asked* are, then :func:`queue_clip`). Raises
    :class:`ClipError` with a message the person reads.
    """
    plan = await plan_clip(db, user, **asked)
    return await queue_clip(db, user, plan, conversation_id=conversation_id, name=name), plan.label


async def queue_clip(db: Any, user: Any, plan: ClipPlan, *, conversation_id: object = None, name: str | None = None):
    """The shot *plan* (:func:`plan_clip`) asks, stored for *user*, flushed and enqueued, not committed.

    A provider that cools (``provider_cooling``) has the shot queued for
    when it cools, its row saying why. The agent tool queues the plan it
    planned, so its answer says the plan's notes (:attr:`ClipPlan.notes`).
    """
    from app.models.video_studio import VideoClip
    from app.services.video_studio import jobs, provider_cooling

    clip = VideoClip(
        user_id=user.id,
        conversation_id=conversation_id,
        name=(name or "").strip()[:200] or None,
        provider=plan.config.provider,
        model=plan.request.model,
        prompt=plan.request.prompt,
        params=plan.params,
    )
    db.add(clip)
    await db.flush()
    cooling = await provider_cooling.cooling_of(plan.config.id, model=plan.request.model)
    when = {"not_before": cooling.until} if cooling is not None else {}
    await jobs.enqueue(db, "clip", clip, owed_to=clip.conversation_id, **when)
    if cooling is not None:
        provider_cooling.hold(clip, cooling)
    return clip


def _named(value: Any) -> bool:
    return value not in (None, "", [])


#: The params key of the similarity under which a take is failed as drifted from its start frame.
MIN_REFERENCE_SIMILARITY = "min_reference_similarity"


def reference_similarity_threshold(value: Any, *, start_frame: bool) -> float | None:
    """The ``min_reference_similarity`` a call asks (0-1; 0 or none is no threshold), or :class:`ClipError`."""
    if value in (None, ""):
        return None
    try:
        threshold = float(value)
    except (TypeError, ValueError):
        raise ClipError("min_reference_similarity is a number from 0 to 1.") from None
    if not 0.0 <= threshold <= 1.0:
        raise ClipError("min_reference_similarity is a number from 0 to 1.")
    if threshold == 0.0:
        return None
    if not start_frame:
        raise ClipError(
            "min_reference_similarity compares the shot's first frame with its start frame: give the shot one "
            "(reference_file_id, reference_recording_id or reference_clip_id)."
        )
    return round(threshold, 3)


def kept_regions_asked(value: Any, *, start_frame: bool) -> list[list[float]] | None:
    """The ``keep_regions`` a call asks: ``None`` unset, ``[]`` none, else boxes; :class:`ClipError` when wrong."""
    from app.services.video_studio.spec import kept_boxes

    if value is None:
        return None
    try:
        boxes = kept_boxes(value)
    except ValueError as exc:
        raise ClipError(f"keep_regions: {exc}.") from None
    if boxes and not start_frame:
        raise ClipError(
            "keep_regions keeps parts of the shot's start picture as they are: give the shot one (reference_file_id, "
            "reference_recording_id or reference_clip_id)."
        )
    return boxes


async def resolve_stray_screens(
    db: Any, user: Any, value: Any, *, negative_prompt: str | None, content_check: bool
) -> dict | None:
    """The ``stray_screens`` a call asks, as the params store it: ``None`` for ``fail`` (the default).

    ``flatten``, or a real screenshot the person can read (``{file_id, fit,
    light}``). It cleans what the content check finds against the negative
    prompt, so it needs both; :class:`ClipError` otherwise.
    """
    from app.services.video_studio import pictures, stray_screens
    from app.services.video_studio.content_drift import excluded_terms

    try:
        option = stray_screens.parse_option(value)
    except ValueError as exc:
        raise ClipError(f"{exc}.") from None
    if option is None:
        return None
    if not excluded_terms(negative_prompt):
        raise ClipError(
            "stray_screens cleans the screens a shot shows against its negative prompt: give the shot a "
            "negative_prompt naming what they must not show (screens, readable text)."
        )
    if not content_check:
        raise ClipError("stray_screens acts on what the content check finds: it needs content_check on.")
    if option["mode"] == stray_screens.SCREENSHOT:
        try:
            await pictures.read_picture(db, user, option["file_id"], what="the screenshot")
        except pictures.PictureError as exc:
            raise ClipError(str(exc)) from None
    return option


#: A ``max_camera_motion`` at or under this asks a locked-off camera: of a model whose provider says it does not
#: hold one (``holds_locked_camera``), the answer says so - and names one that does - before the take is paid for.
LOCKED_CAMERA_MOTION = 0.5


async def _holding_camera(db: Any, user: Any, *, start_frame: bool, besides: tuple[str, str]) -> tuple | None:
    """The first video model *user* may use whose provider says it holds a locked-off camera: ``(provider, model, config)``.

    Read from the models ``video_studio_capabilities`` lists, *besides* (``(provider_id, model)``) left out.
    """
    from app.llm.router import list_video_models_for

    for entry in await list_video_models_for(db, user_id=user.id):
        if (str(entry.get("provider_id")), entry.get("model")) == besides:
            continue
        try:
            provider, model, config = await resolve(db, user.id, model=entry.get("model"), provider_id=entry.get("provider_id"))
        except ClipError:
            continue
        if provider.holds_locked_camera(model, start_frame=start_frame):
            return provider, model, config
    return None


async def locked_camera(db: Any, user: Any, resolved: tuple, *, tolerance: float | None, start_frame: bool) -> list[str]:
    """What to say of a shot asking *tolerance* of camera motion of the model *resolved*.

    A locked-off camera (*tolerance* at or under :data:`LOCKED_CAMERA_MOTION`)
    asked of a model whose provider says it does not hold one - with or
    without a start frame (*start_frame*) - is still asked of that model (one
    model per round): the answer says it will not hold it, and which
    model would, before anything is paid.
    """
    provider, model, config = resolved
    if tolerance is None or tolerance > LOCKED_CAMERA_MOTION or provider.holds_locked_camera(model, start_frame=start_frame) is not False:
        return []
    other = await _holding_camera(db, user, start_frame=start_frame, besides=(str(config.id), model))
    without = "" if start_frame else " from a prompt without a start frame"
    remedies = [] if start_frame else ["give it a start frame (reference_*)"]
    if other is not None:
        remedies.append(f"name {other[1]}, which holds one")
    remedy = f" - {' or '.join(remedies)}" if remedies else ""
    return [
        f"{model} does not hold a locked-off camera{without}: with max_camera_motion {tolerance:.2f}, its take is "
        f"likely to fail its camera check and be paid for anyway{remedy}."
    ]


def camera_motion_tolerance(value: Any) -> float | None:
    """The ``max_camera_motion`` a call asks (0-1: how much the last frame may change from the first; 1 is none)."""
    if value in (None, ""):
        return None
    try:
        tolerance = float(value)
    except (TypeError, ValueError):
        raise ClipError("max_camera_motion is a number from 0 to 1.") from None
    if not 0.0 <= tolerance <= 1.0:
        raise ClipError("max_camera_motion is a number from 0 to 1.")
    return None if tolerance >= 1.0 else round(tolerance, 3)


#: The params key of a shot's stabilisation: ``True`` always, ``False`` never; unset, a shot asked with
#: ``max_camera_motion`` or whose prompt asks for a locked camera is stabilised (``stabilise``).
STABILISE = "stabilise"


def stabilise_choice(value: Any) -> bool | None:
    """The ``stabilise`` a call asks: ``True``, ``False``, or ``None`` (automatic), or :class:`ClipError`."""
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return value
    raise ClipError("stabilise is true (always), false (never), or left out (when the camera is asked still).")


def wants_stabilising(params: dict, camera: str | None) -> bool:
    """Whether a take is stabilised: asked so, else when it has a ``max_camera_motion`` or its prompt asks a locked camera.

    *camera* is the camera the prompt asks for (``content_drift.CAMERAS``), read from the prompt, never a phrase list.
    """
    from app.services.video_studio import content_drift, frame_qc

    choice = params.get(STABILISE)
    if isinstance(choice, bool):
        return choice
    return params.get(frame_qc.MAX_CAMERA_MOTION) is not None or camera == content_drift.CAMERA_LOCKED


# ── Another model when a take fails its checks ────────────────────

#: The params keys of the models a shot falls back to, in order (``{model, provider_id, provider}``), and of the
#: models whose take failed in this chain of attempts (``{model, provider, reason}``), oldest first.
FALLBACK_MODELS = "fallback_models"
FALLBACK_TRIED = "fallback_tried"
MAX_FALLBACK_MODELS = 3


def parse_fallback_models(raw: Any, *, own: str | None = None) -> list[str]:
    """The model names a call's ``fallback_models`` gives, in order, once each, or :class:`ClipError`.

    *own*, the shot's own model, is no fallback: named among them, it is not counted against
    :data:`MAX_FALLBACK_MODELS` (:func:`resolve_fallbacks` drops it).
    """
    if raw in (None, ""):
        return []
    items = raw if isinstance(raw, list | tuple) else [raw]
    names = [str(item).strip() for item in items if isinstance(item, str) and item.strip()]
    if len(names) != len(items):
        raise ClipError("fallback_models is a list of model names from video_studio_capabilities.")
    names = list(dict.fromkeys(names))
    if len([name for name in names if name != own]) > MAX_FALLBACK_MODELS:
        raise ClipError(f"A shot falls back to at most {MAX_FALLBACK_MODELS} other models.")
    return names


def _check_fallback(provider: Any, model: str, params: dict) -> None:
    """Refuse a fallback *model* that cannot make the shot as *params* ask it: its reference images, its ingredients."""
    from app.llm.router import video_reference_limits

    started = starts_from_picture(params)
    ingredients = ingredients_of(params)
    if ingredients:
        path = _path_of(provider, model, ingredients, start_frame=started)
        if needs_start_frame(ingredients, path) and not started:
            raise ClipError(
                f"fallback_models: {model} takes no reference images for this shot, and the shot has no start frame "
                "showing its ingredients: pick a model with reference_images in video_studio_capabilities.",
                about_model=True,
            )
        return
    references = len(params.get(REFERENCE_IMAGES) or [])
    try:
        check_reference_combination(
            video_reference_limits(provider, model), model, references=references, start_frame=started
        )
    except ClipError as exc:
        raise ClipError(f"fallback_models: {exc}", about_model=True) from None


async def resolve_fallbacks(
    db: Any,
    user: Any,
    raw: Any,
    *,
    model: str,
    params: dict,
    again: bool = False,
    configured: tuple[str | None, list[str]] | None = None,
) -> tuple[list[dict], list[str]]:
    """The fallback models a shot asked of *model* tries in order, each resolved and checked against the shot,
    and what the answer says of them.

    Each is a model the person may use (``router.get_video_provider``), that
    its provider serves (one it answered HTTP 404 for is refused as
    ``fallback B is not served by PROVIDER``), able to take the shot as asked
    (:func:`_check_fallback`): refused before anything is paid, naming the
    model. The shot's own model named among them
    is no fallback - its first take is made with it -: dropped, the others
    kept, and the notes say so with what is left. A list naming no model at
    all asks a fallback of none: refused on a new shot, while on a shot
    generated again (*again*) ``[]`` drops its fallbacks.

    *configured* ``(default, fallbacks)`` (:func:`configured_models`, read
    when not given) holds the chain to the platform's configuration once it
    sets a default video model: no fallback named, the configured video
    fallback models are the chain (one that cannot take the shot skipped,
    said in the notes); a fallback outside them is refused, naming the
    configuration.
    """
    default, allowed = configured if configured is not None else await configured_models(db)
    implicit = raw in (None, "")
    if implicit:
        raw = [name for name in allowed if name != model][:MAX_FALLBACK_MODELS] if default is not None else []
        if not raw:
            return [], []
    names = parse_fallback_models(raw, own=model)
    if default is not None and (outside := [name for name in names if name != model and name not in allowed]):
        raise ClipError(
            f"fallback_models: {', '.join(outside)} not allowed by configuration: a shot falls back only to the "
            + (f"configured video fallback models ({', '.join(allowed)})" if allowed else "configured video fallback models, and none is configured")
            + f". Leave fallback_models out, or change {VIDEO_MODELS_SETTING}",
            about_model=True,
        )
    if not names:
        if again:
            return [], []
        raise ClipError(
            "fallback_models names no model: name 1-3 models from video_studio_capabilities to try after the "
            "shot's own, or leave it out"
        )
    resolved: list[dict] = []
    dropped: list[str] = []
    skipped: list[str] = []
    for name in names:
        if name == model:
            dropped.append(name)
            continue
        try:
            provider, chosen, config = await resolve(db, user.id, model=name)
            if chosen == model:
                dropped.append(name)
                continue
            _check_fallback(provider, chosen, params)
        except ClipError as exc:
            if implicit:
                # A configured fallback this shot cannot use: the chain goes on without it, said.
                skipped.append(f"{name} ({str(exc).rstrip('. ')})")
                continue
            if exc.not_served is None:
                raise
            raise ClipError(
                f"fallback_models: fallback {exc.not_served}: pick another model video_studio_capabilities lists",
                not_served=exc.not_served,
            ) from None
        label = getattr(config, "display_name", None) or config.provider
        resolved.append({"model": chosen, "provider_id": str(config.id), "provider": label})
    if implicit:
        chain = ", then ".join(item["model"] for item in resolved)
        said = [f"Configured video fallback: {chain}, tried in order if the default model fails."] if resolved else []
        if skipped:
            said.append(f"Configured video fallback not used for this shot: {'; '.join(skipped)}.")
        return resolved, said
    if not dropped:
        return resolved, []
    left = (
        f"fallback {', then '.join(item['model'] for item in resolved)}"
        if resolved
        else "no fallback: a take failing a check of its frames fails the shot"
    )
    return resolved, [f"fallback_models: {dropped[0]} is the shot's own model, dropped (its first take is made with it); {left}."]


def next_fallback(params: dict, current_model: str | None) -> dict | None:
    """The fallback model a shot whose take failed tries next: the first not tried in this chain, nor the one that failed."""
    tried = {str(item.get("model")) for item in params.get(FALLBACK_TRIED) or [] if isinstance(item, dict)}
    for item in params.get(FALLBACK_MODELS) or []:
        if isinstance(item, dict) and item.get("model") and item["model"] not in tried and item["model"] != current_model:
            return item
    return None


def fallback_state(clip: Any) -> dict | None:
    """What the model fallback did for *clip*: the models asked, those that failed and why, which made the take."""
    from app.models.video_studio import JOB_READY

    params = getattr(clip, "params", None) or {}
    models = [str(item.get("model")) for item in params.get(FALLBACK_MODELS) or [] if isinstance(item, dict)]
    tried = [item for item in params.get(FALLBACK_TRIED) or [] if isinstance(item, dict)]
    if not models and not tried:
        return None
    return {
        "models": models,
        "tried": [{"model": item.get("model"), "provider": item.get("provider"), "reason": item.get("reason")} for item in tried],
        "made_by": clip.model if clip.status == JOB_READY else None,
        "fell_back": bool(tried),
    }


def fallback_note(clip: Any) -> str | None:
    """The sentence an answer says which model made the take in; ``None`` when the shot asked no fallback."""
    state = fallback_state(clip)
    if state is None or not state["tried"]:
        return None
    failed = "; ".join(f"{item['model']}: {str(item['reason'] or 'no reason given').rstrip('. ')[:200]}" for item in state["tried"])
    if state["made_by"]:
        return f"The take kept was made by {state['made_by']}, a fallback model, after {failed}."
    return f"Every model tried failed its checks: {failed}."


async def generate(
    provider: Any,
    request: VideoRequest,
    *,
    progress: Callable[[int], Awaitable[None]] | None = None,
    on_job: Callable[[str], Awaitable[None]] | None = None,
    resume_job: str | None = None,
    on_state: Callable[[VideoJobState], Awaitable[None]] | None = None,
) -> VideoResult:
    """The clip's video, from *provider*; every failure as :class:`ClipError`.

    A provider that cannot take the request now (:class:`VideoProviderBusyError`)
    and an unexpected failure (a crash mid-poll) are let through: the job asks
    again after a back-off, and since *on_job* stored the upstream job the
    moment it existed, that retry resumes it (*resume_job*) instead of paying
    for a second video. *on_state* hears what the provider says of
    that job at every poll. A provider answering HTTP 404 for the
    model (:class:`VideoModelNotServedError`) is said as such (``not_served``):
    the worker records it, so the next listing drops the model.
    """
    from app.llm import video_served

    try:
        return await provider.generate_video(
            request, progress=progress, on_job=on_job, resume_job=resume_job, on_state=on_state
        )
    except VideoModelNotServedError as exc:
        label = getattr(provider, "provider_config_label", None) or getattr(provider, "provider_name", None) or "its provider"
        raise ClipError(str(exc), not_served=video_served.sentence(request.model, label, exc.detail)) from None
    except VideoRequestRefusedError as exc:
        # Refused on its arguments before a job existed: nothing made, nothing paid.
        raise ClipError(str(exc), unpaid=True) from None
    except VideoGenerationError as exc:
        raise ClipError(str(exc)) from None
    except NotImplementedError:
        raise ClipError(f"{provider.provider_name} does not generate video.") from None


def ingredients_quality_asked(asked: Any) -> dict:
    """The params a shot keeps of its ``ingredients_quality``: ``{"ingredients_quality": "flat"}``, or nothing.

    ``"flat"`` accepts a logo drawn onto the take (composited or tracked)
    that does not read as part of its surface: its logo check records the
    integration, never fails it (``logo_integration``).
    ``"integrated"`` (the default) or nothing
    keeps the gate; anything else is refused.
    """
    from app.services.video_studio import logo_integration

    if asked in (None, ""):
        return {}
    value = str(asked).strip().lower()
    if value == logo_integration.QUALITY_FLAT:
        return {logo_integration.QUALITY_KEY: logo_integration.QUALITY_FLAT}
    if value == "integrated":
        return {}
    raise ClipError(
        f'ingredients_quality is "integrated" (the default: a logo drawn onto the take must read as part of its '
        f'surface) or "{logo_integration.QUALITY_FLAT}" (a logo drawn flat accepted), not {asked!r}.'
    )


#: What a shot generated again may change; the rest is the shot as it was asked.
RETRY_FIELDS: tuple[str, ...] = (
    "prompt",
    "model",
    "provider_id",
    "duration_seconds",
    "aspect_ratio",
    "resolution",
    "negative_prompt",
    "sound",
    "text_check",
    "content_check",
    "accept_minor_drift",
    # "flat" accepts a logo drawn flat; "integrated" drops it.
    "ingredients_quality",
    "reference_image_file_ids",
    # A new start: a source resolved as ``start_clip`` resolves it, or the kept video at another moment.
    *START_FIELDS,
    # ``False`` drops the kept start frame (its file stays in the conversation); ``True`` keeps it.
    "start_frame",
    "ingredients",
    "min_reference_similarity",
    # The video's references the shot is held to: [] drops them.
    "consistency",
    # How much its camera may move: 1 drops it.
    "max_camera_motion",
    # Regions of the start picture kept as they are, [] drops them; the stray screens' cleanup, "fail" drops it.
    "keep_regions",
    "stray_screens",
    # Whether a drifting take is stabilised, and the models tried after it when a take fails its checks.
    "stabilise",
    "fallback_models",
    # The video or template whose art direction the new take is made under; none named: the shot's own.
    "video_id",
    "template_id",
    "freestyle",
    # A new quad or screenshot of a screen replacement, and whether the shot carries a screen insert.
    "screen",
    "insert",
    # ``True`` drops a ready shot's take now instead of keeping it until the new one is ready.
    "discard",
    # How a reference image outside the model's accepted shape is fitted: "refuse" or "pad".
    "reference_fit",
)
#: How many times one shot is generated again from the same prompt and start frame: each attempt is a
#: paid generation. A soft limit: a new prompt or a new start frame starts the count again.
MAX_RETRIES = 5
#: The params key of that count: ``{"key": <the prompt and start frame's digest>, "count": n}``.
REGENERATIONS = "regenerations"
#: What a retry may change and still not be another try of the same shot: the model that makes it and how
#: still its camera must hold. Such a retry is never counted, nor refused.
UNCOUNTED_CHANGES = frozenset({"model", "provider_id", "max_camera_motion"})
#: What a retry names that says nothing of the shot generated: the video it is made for, what becomes of its take.
_CONTEXT_CHANGES = frozenset({"video_id", "template_id", "freestyle", "discard"})


def regeneration_key(prompt: str, params: dict) -> str:
    """The digest a shot's retries are counted under: its prompt (spacing aside) and the start frame it is made from."""
    import hashlib
    import json

    start = {"file": params.get("reference_file_id"), "video": params.get("reference_video")}
    raw = json.dumps({"prompt": " ".join((prompt or "").split()), "start": start}, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def counts_toward_cap(changes: dict) -> bool:
    """Whether a retry asking *changes* is another try of the same shot: anything beyond :data:`UNCOUNTED_CHANGES`.

    A retry asking nothing new is one; a retry asking a model or a camera tolerance only is not.
    """
    asked = {key for key, value in changes.items() if value is not None and key not in _CONTEXT_CHANGES}
    return not (asked and asked <= UNCOUNTED_CHANGES)


def count_regeneration(params: dict, *, prompt: str, changes: dict) -> None:
    """Count a retry in *params* (:data:`REGENERATIONS`), or :class:`ClipError` past :data:`MAX_RETRIES` tries of it.

    The count is per prompt and start frame (:func:`regeneration_key`), not per shot: a new prompt or a new
    start frame starts it again. A retry :func:`counts_toward_cap` says is no new try is neither counted nor refused.
    """
    key = regeneration_key(prompt, params)
    before = params.get(REGENERATIONS) if isinstance(params.get(REGENERATIONS), dict) else {}
    count = int(before.get("count") or 0) if before.get("key") == key else 0
    counted = counts_toward_cap(changes)
    if counted:
        if count >= MAX_RETRIES:
            raise ClipError(
                f"This shot was generated again {MAX_RETRIES} times from the same prompt and start frame: change its "
                "prompt or its start frame (the count starts again), or ask another model or max_camera_motion - "
                "a retry changing only those is never counted."
            )
        count += 1
    # ``last``: this round counted a try, which a refusal before anything is paid gives back.
    params[REGENERATIONS] = {"key": key, "count": count, **({"last": True} if counted else {})}


def uncount_regeneration(params: dict, prompt: str) -> bool:
    """Give back the try a shot refused before anything was paid counted: ``True`` when one was.

    A provider refusing the request on its arguments (HTTP 4xx before a job
    existed), or a picture refused on its shape at the worker, made nothing:
    it is no try of the prompt and start frame :func:`count_regeneration`
    counted. The next ``validate_only`` shows the count unchanged.
    """
    before = params.get(REGENERATIONS) if isinstance(params.get(REGENERATIONS), dict) else {}
    count = int(before.get("count") or 0)
    if not before.get("last") or before.get("key") != regeneration_key(prompt, params) or count <= 0:
        return False
    params[REGENERATIONS] = {"key": before["key"], "count": count - 1}
    return True


def attempt_clause(params: dict, prompt: str) -> str:
    """Which attempt a take that failed its content check was, and how many of the :data:`MAX_RETRIES` regenerations of
    its *prompt* and start frame remain - what a failure's reason ends with."""
    before = params.get(REGENERATIONS) if isinstance(params.get(REGENERATIONS), dict) else {}
    used = int(before.get("count") or 0) if before.get("key") == regeneration_key(prompt, params) else 0
    left = max(0, MAX_RETRIES - used)
    said = (
        f" Attempt {_attempt(params) + 1} of this shot: {left or 'none'} of the {MAX_RETRIES} regenerations of this "
        "prompt and start frame remain"
    )
    if left:
        return f"{said}."
    return f"{said} - change its prompt or its start frame (the count starts again), or ask another model."


#: The params key holding the media a shot generated again no longer plays (a failed take, a take
#: discarded): deleted once the new generation ends (``runners.run_clip``), or with the shot.
REPLACED_MEDIA = "replaced_media"


# ── The take a ready shot keeps while it is generated again ──────

#: The params key of the take a ready shot keeps playing while it is generated again: its video, poster,
#: size and length stay on the row's columns, its frames and notes wait here while an attempt runs (put
#: back if it fails); what made it (``prompt``, ``model``, ``made_at``) stays until a new take replaces it.
KEPT_TAKE = "kept_take"
#: The params key of what the attempt in flight made beside the kept take: its video once downloaded (a
#: later run takes it, never asks again), its poster, size and length.
ATTEMPT_MEDIA = "attempt_media"
#: The params key of the regenerations that failed since the take that plays, newest last: each its
#: status, reason and when, and the video, poster and frames it made, if any.
ATTEMPTS = "attempts"
#: The row's columns a take's media are.
TAKE_MEDIA = ("file_id", "poster_file_id", "duration_ms", "width", "height", "has_audio")
_TAKE_FILES = ("file_id", "poster_file_id")


def keeps_take(params: dict | None) -> bool:
    """Whether the row's media are a ready take kept while the shot is, or was, generated again."""
    return isinstance((params or {}).get(KEPT_TAKE), dict)


def playable(clip: Any) -> bool:
    """Whether *clip* has a take to play: ready, or keeping its ready take while generated again; media not expired.

    The one rule the render, the preview and the quality check read a shot's footage by.
    """
    from app.models.video_studio import JOB_READY

    if not clip.file_id or getattr(clip, "expired_at", None) is not None:
        return False
    return clip.status == JOB_READY or keeps_take(getattr(clip, "params", None))


def failed_regeneration(clip: Any) -> dict | None:
    """The last regeneration of a ready shot when it failed, its previous take still playing; ``None`` otherwise."""
    from app.models.video_studio import JOB_READY

    params = getattr(clip, "params", None) or {}
    attempts = params.get(ATTEMPTS)
    if clip.status != JOB_READY or not keeps_take(params) or not isinstance(attempts, list) or not attempts:
        return None
    last = attempts[-1]
    return last if isinstance(last, dict) else None


def regeneration_state(clip: Any) -> dict | None:
    """``{state: running|failed, reason, attempts}`` of a ready shot generated again; ``None`` for any other."""
    from app.models.video_studio import JOB_PENDING, JOB_RUNNING

    params = getattr(clip, "params", None) or {}
    if not keeps_take(params) or not getattr(clip, "file_id", None):
        return None
    count = len(params.get(ATTEMPTS) or [])
    if clip.status in (JOB_PENDING, JOB_RUNNING):
        return {"state": "running", "reason": None, "attempts_failed": count, "previous_take_plays": True}
    failed = failed_regeneration(clip)
    if failed is None:
        return None
    return {"state": "failed", "reason": failed.get("reason"), "attempts_failed": count, "previous_take_plays": True}


def regeneration_reason(failed: dict) -> str:
    """``last regeneration failed: <reason>, the previous take plays``."""
    reason = str(failed.get("reason") or "no reason given").rstrip(". ")
    return f"last regeneration failed: {reason}, the previous take plays"


def take_params(params: dict | None) -> dict:
    """*params* as the take that plays reads them: its own frames and notes while an attempt runs beside it."""
    from app.services.video_studio import frame_qc

    params = params or {}
    kept = params.get(KEPT_TAKE)
    if not isinstance(kept, dict) or not ({"frame_qc", "notes"} & kept.keys()):
        return params
    view = {key: value for key, value in params.items() if key not in (frame_qc.QC_KEY, "notes")}
    if isinstance(kept.get("frame_qc"), dict):
        view[frame_qc.QC_KEY] = kept["frame_qc"]
    if kept.get("notes"):
        view["notes"] = kept["notes"]
    return view


def keep_take(params: dict, clip: Any) -> None:
    """Keep *clip*'s ready take playing while it is generated again: its frames and notes set aside in *params*."""
    from app.services.video_studio import frame_qc

    kept = params.get(KEPT_TAKE)
    if not isinstance(kept, dict):
        kept = {
            "prompt": clip.prompt,
            "model": clip.model,
            "made_at": clip.finished_at.isoformat() if clip.finished_at else None,
        }
    params[KEPT_TAKE] = {
        **{key: value for key, value in kept.items() if key not in ("frame_qc", "notes")},
        "frame_qc": params.pop(frame_qc.QC_KEY, None),
        "notes": params.pop("notes", None),
    }


def _plain(media: dict) -> dict:
    """Media columns as JSON holds them: file ids as text."""
    return {key: (str(value) if key in _TAKE_FILES and value else value) for key, value in media.items() if value is not None}


def keep_attempt_media(params: dict, media: dict) -> None:
    """Record in *params* what the attempt in flight made, beside the kept take (:data:`ATTEMPT_MEDIA`)."""
    params[ATTEMPT_MEDIA] = {**(params.get(ATTEMPT_MEDIA) or {}), **_plain(media)}


def attempt_file(params: dict) -> Any:
    """The video the attempt in flight already downloaded beside the kept take, as the row's column takes it."""
    import uuid

    stored = (params.get(ATTEMPT_MEDIA) or {}).get("file_id")
    try:
        return uuid.UUID(str(stored)) if stored else None
    except ValueError:
        return None


def _attempt_files(attempt: dict) -> list[str]:
    from app.services.video_studio import frame_qc

    return [
        *(str(attempt[key]) for key in _TAKE_FILES if attempt.get(key)),
        *frame_qc.frame_file_ids({frame_qc.QC_KEY: attempt.get("frame_qc")}),
    ]


def _beside_take(params: dict) -> list[str]:
    """The files the params keep beside the row's media: the kept take's frames, the attempts', the one in flight."""
    from app.services.video_studio import frame_qc

    kept = params.get(KEPT_TAKE) if isinstance(params.get(KEPT_TAKE), dict) else {}
    files = frame_qc.frame_file_ids({frame_qc.QC_KEY: kept.get("frame_qc")})
    for attempt in [*(params.get(ATTEMPTS) or []), *previous_takes(params)]:
        if isinstance(attempt, dict):
            files.extend(_attempt_files(attempt))
    files.extend(_attempt_files(params.get(ATTEMPT_MEDIA) or {}))
    return files


def side_files(params: dict | None, *, videos_only: bool = False) -> list[str]:
    """The files a shot's params own beside its row's media, once each.

    The take being replaced (:data:`REPLACED_MEDIA`), the kept take's frames, the failed attempts' and the
    attempt in flight's videos, posters and frames. *videos_only*: the take being replaced and the videos -
    what the retention's expiry deletes, a poster and frames outliving the media; an un-stabilised take kept
    beside its frames is a video.
    """
    from app.services.video_studio import frame_qc

    params = params or {}
    files = [str(fid) for fid in params.get(REPLACED_MEDIA) or [] if fid]
    if videos_only:
        attempts = [a for a in params.get(ATTEMPTS) or [] if isinstance(a, dict)] + previous_takes(params)
        files.extend(str(a["file_id"]) for a in [*attempts, params.get(ATTEMPT_MEDIA) or {}] if a.get("file_id"))
        kept = params.get(KEPT_TAKE) if isinstance(params.get(KEPT_TAKE), dict) else {}
        for qc in (params.get(frame_qc.QC_KEY), kept.get("frame_qc"), *(a.get("frame_qc") for a in attempts)):
            files.extend(frame_qc.video_file_ids(qc))
    else:
        files.extend(_beside_take(params))
    return list(dict.fromkeys(files))


def owned_files(clip: Any) -> list[str]:
    """Every file a shot owns: its media, poster and frames, and what its params keep beside them (:func:`side_files`)."""
    from app.services.video_studio import frame_qc

    files = [str(fid) for fid in (clip.file_id, clip.poster_file_id) if fid]
    return list(dict.fromkeys([*files, *frame_qc.frame_file_ids(clip.params), *side_files(clip.params)]))


def end_attempt(params: dict, *, status: str, reason: str, made: dict | None = None) -> dict[str, Any]:
    """The columns an attempt that made no ready take ends with; *params* updated in place.

    A shot keeping a ready take stays ready on it - nothing paid is asked again: the attempt (its status,
    reason, and the video, poster and frames it made) goes to :data:`ATTEMPTS` beside it, and the take's
    frames and notes come back. Any other ends *status* with *reason*, the media it made (*made*) on the
    row to look at.
    """
    from app.models.video_studio import JOB_READY
    from app.services.video_studio import frame_qc

    if not keeps_take(params):
        return {"status": status, "error": reason[:2000], **(made or {})}
    media = {**(params.pop(ATTEMPT_MEDIA, None) or {}), **_plain({k: v for k, v in (made or {}).items() if k in TAKE_MEDIA})}
    attempt: dict[str, Any] = {
        "attempt": params.get("attempt"),
        "status": status,
        "reason": reason[:2000],
        "at": datetime.now(UTC).isoformat(),
        **{key: media[key] for key in _TAKE_FILES if media.get(key)},
        # Its size and length too: an attempt promoted to the take (:func:`promote_attempt`) plays with them.
        **{key: media[key] for key in TAKE_MEDIA if key not in _TAKE_FILES and media.get(key) is not None},
    }
    frames = params.pop(frame_qc.QC_KEY, None)
    if isinstance(frames, dict):
        attempt["frame_qc"] = frames
    notes = params.pop("notes", None)
    if notes:
        attempt["notes"] = notes
    params[ATTEMPTS] = [*(params.get(ATTEMPTS) or []), attempt]
    kept = dict(params[KEPT_TAKE])
    if isinstance(kept.get("frame_qc"), dict):
        params[frame_qc.QC_KEY] = kept["frame_qc"]
    if kept.get("notes"):
        params["notes"] = kept["notes"]
    params[KEPT_TAKE] = {key: value for key, value in kept.items() if key not in ("frame_qc", "notes")}
    return {"status": JOB_READY, "error": None, "progress": 100, "stage": None}


#: The params key of the failed attempt a person promoted to the shot's take (:func:`promote_attempt`): its file,
#: when, the reason it had failed and the findings it was kept with.
ACCEPTED_ATTEMPT = "accepted_attempt"


def _inside(box: Any, covered: tuple[float, float, float, float]) -> bool:
    """Whether a word's *box* (``[x, y, width, height]``, shares) lies inside *covered* (``x0, x1, y0, y1``), its
    centre at least, with a hundredth of slack for the reading's rounding."""
    if not isinstance(box, list | tuple) or len(box) != 4:
        return False
    x, y, width, height = (float(v) for v in box)
    cx, cy = x + width / 2.0, y + height / 2.0
    x0, x1, y0, y1 = covered
    return x0 - 0.01 <= cx <= x1 + 0.01 and y0 - 0.01 <= cy <= y1 + 0.01


def attempt_findings_outside(record: dict | None, covered: tuple[float, float, float, float] | None) -> list[str]:
    """The findings of a take's text check (``frame_qc.TEXT_CHECK``) that do NOT lie on the screen *covered* by its
    insert, as the refusal names them: a failed word whose box is outside or unknown, and each invented word outside.
    No covered screen: every finding is outside."""
    record = record if isinstance(record, dict) else {}
    out: list[str] = []
    failure = record.get("failure")
    if isinstance(failure, dict) and (covered is None or not _inside(failure.get("box"), covered)):
        out.append(f'"{failure.get("word")}" reads "{failure.get("seen")}" ({str(failure.get("frame") or "").replace("_", " ")})')
    for item in record.get("invented") or []:
        if isinstance(item, dict) and (covered is None or not _inside(item.get("box"), covered)):
            out.append(f'invented "{item.get("word")}" ({str(item.get("frame") or "").replace("_", " ")})')
    return out


def promote_attempt(clip: Any, file_id: str) -> tuple[dict[str, Any], list[str]]:
    """The row's columns and the notes of promoting the failed take *file_id* of *clip* to its take, after review.

    The take is a failed regeneration kept beside the take that plays (:data:`ATTEMPTS`), or the media of a shot
    whose generation failed. Only a take that failed on its text check is promoted, and only when every finding of
    that check lies on the screen its screen insert covers (``insert_keep_out.covered_box``,
    :func:`attempt_findings_outside`): lettering the insert draws over. The take that played goes to
    :data:`REPLACED_MEDIA` (deleted at the next generation, or with the shot), the attempt's invented words under the
    insert leave its record (the render is not refused on them) and :data:`ACCEPTED_ATTEMPT` records the promotion.
    ``ClipError`` names why it is refused.
    """
    from app.models.video_studio import JOB_FAILED, JOB_READY
    from app.services.video_studio import frame_qc, insert_keep_out

    params = dict(getattr(clip, "params", None) or {})
    wanted = str(file_id).strip()
    attempts = [a for a in params.get(ATTEMPTS) or [] if isinstance(a, dict)]
    attempt = next((a for a in attempts if str(a.get("file_id") or "") == wanted), None)
    on_row = attempt is None and clip.status == JOB_FAILED and str(clip.file_id or "") == wanted
    if attempt is None and not on_row:
        raise ClipError(
            f"`{wanted}` is no failed take of this shot: accept_attempt takes the file_id of a failed_attempt its "
            "answer lists, or of the failed take on a failed shot."
        )
    frames = (attempt.get("frame_qc") if attempt is not None else params.get(frame_qc.QC_KEY)) or {}
    reason = str((attempt.get("reason") if attempt is not None else getattr(clip, "error", None)) or "")
    record = frames.get(frame_qc.TEXT_CHECK) if isinstance(frames, dict) else None
    if not (isinstance(record, dict) and record.get("status") == "failed") and not reason.startswith("text drift"):
        raise ClipError(
            "Only a take that failed its text check can be accepted; this one failed for another reason "
            f"({reason[:200] or 'none recorded'}): generate the shot again."
        )
    covered = insert_keep_out.covered_box(params)
    if covered is None and isinstance(frames, dict):
        covered = insert_keep_out.covered_box({**params, frame_qc.QC_KEY: frames})
    outside = attempt_findings_outside(record, covered)
    if covered is None:
        raise ClipError(
            "Only lettering a screen insert covers can be accepted, and this shot carries no screen insert "
            "(insert: true, or a scene drawing a screen_insert on it): generate the shot again."
        )
    if outside:
        raise ClipError(
            "The take has text findings outside the screen its insert covers ("
            f"{insert_keep_out.spans(covered)}): {', '.join(outside[:6])}. Only lettering under the insert can be "
            "accepted: generate the shot again."
        )
    at = datetime.now(UTC).isoformat()
    kept_record = dict(record) if isinstance(record, dict) else {}
    under = [i for i in kept_record.get("invented") or [] if isinstance(i, dict)]
    if kept_record:
        kept_record.update(status="accepted", invented=[], under_insert=under, covered_by_insert=list(covered))
    note = (
        f"Accepted after review ({at[:16]}Z): this take had failed its text check on lettering the screen insert "
        f"covers ({insert_keep_out.spans(covered)}) and was promoted to the shot's take."
    )
    accepted = {"file_id": wanted, "at": at, "reason": reason[:2000], "covered": list(covered), "under_insert": under}
    columns: dict[str, Any]
    if on_row:
        if kept_record:
            params[frame_qc.QC_KEY] = {**frames, frame_qc.TEXT_CHECK: kept_record}
        params["notes"] = [*(params.get("notes") or []), note]
        params[ACCEPTED_ATTEMPT] = accepted
        columns = {"status": JOB_READY, "error": None, "progress": 100, "stage": None, "params": params}
        return columns, [note]
    kept = params.get(KEPT_TAKE) if isinstance(params.get(KEPT_TAKE), dict) else {}
    replaced = [str(fid) for fid in (clip.file_id, clip.poster_file_id) if fid]
    replaced += frame_qc.frame_file_ids({frame_qc.QC_KEY: params.get(frame_qc.QC_KEY) or kept.get("frame_qc")})
    params[REPLACED_MEDIA] = list(dict.fromkeys([*(str(f) for f in params.get(REPLACED_MEDIA) or [] if f), *replaced]))
    params[ATTEMPTS] = [a for a in attempts if a is not attempt]
    params.pop(KEPT_TAKE, None)
    params.pop(ATTEMPT_MEDIA, None)
    params[frame_qc.QC_KEY] = {**frames, **({frame_qc.TEXT_CHECK: kept_record} if kept_record else {})}
    params["notes"] = [*(attempt.get("notes") or []), note]
    params[ACCEPTED_ATTEMPT] = accepted
    media = {key: attempt.get(key) for key in TAKE_MEDIA if attempt.get(key) is not None}
    columns = {**media, "status": JOB_READY, "error": None, "progress": 100, "stage": None, "params": params}
    return columns, [note]


def retire_kept_take(params: dict, playing: list, *, media: dict | None = None) -> list[str]:
    """The files to delete once a new ready take replaces the kept one; *params* cleared.

    *playing* is the row's media the kept take played from (its video and poster); *media*, its media columns
    (:data:`TAKE_MEDIA`). Given *media*, the kept take is not deleted: it joins :data:`PREVIOUS_TAKES` (its
    frames and notes with it) for ``restore_take`` to put back, and only the takes past
    :data:`MAX_PREVIOUS_TAKES` go. The attempts that failed beside it go in any case. Only called once the new
    take is ready and passed its checks.
    """
    from app.services.video_studio import frame_qc

    if not keeps_take(params):
        return []
    kept = params.pop(KEPT_TAKE)
    files: list[str] = []
    if media and media.get("file_id"):
        files.extend(keep_previous_take(params, media, kept))
    else:
        files.extend([*(str(fid) for fid in playing if fid), *frame_qc.frame_file_ids({frame_qc.QC_KEY: kept.get("frame_qc")})])
    for attempt in params.pop(ATTEMPTS, None) or []:
        if isinstance(attempt, dict):
            files.extend(_attempt_files(attempt))
    params.pop(ATTEMPT_MEDIA, None)
    return list(dict.fromkeys(files))


# ── The takes a shot played before its take, kept to be put back ──────

#: The params key of the takes a shot played before the one that plays, newest last: each its media
#: (:data:`TAKE_MEDIA`), frames and notes, what made it (``prompt``, ``model``, ``made_at``) and when it was
#: replaced (``replaced_at``). ``restore_take`` puts one back (:func:`restore_previous_take`).
PREVIOUS_TAKES = "previous_takes"
#: How many previous takes a shot keeps; the oldest past it is deleted when a new take replaces one.
MAX_PREVIOUS_TAKES = 3


def previous_takes(params: dict | None) -> list[dict]:
    """The previous takes *params* keep (:data:`PREVIOUS_TAKES`), oldest first."""
    return [entry for entry in (params or {}).get(PREVIOUS_TAKES) or [] if isinstance(entry, dict) and entry.get("file_id")]


def keep_previous_take(params: dict, media: dict, made: dict | None = None) -> list[str]:
    """Keep the take of *media* (its frames and notes in *made*, else in *params*) in :data:`PREVIOUS_TAKES`.

    Returns the files of the takes past :data:`MAX_PREVIOUS_TAKES`, the oldest, to delete.
    """
    from app.services.video_studio import frame_qc

    made = made if isinstance(made, dict) else {}
    entry: dict[str, Any] = {
        **_plain({key: media.get(key) for key in TAKE_MEDIA}),
        **{key: made[key] for key in ("prompt", "model", "made_at") if made.get(key) is not None},
        "replaced_at": datetime.now(UTC).isoformat(),
    }
    frames = made.get("frame_qc") if "frame_qc" in made else params.get(frame_qc.QC_KEY)
    if isinstance(frames, dict):
        entry["frame_qc"] = frames
    notes = made.get("notes") if "notes" in made else params.get("notes")
    if notes:
        entry["notes"] = list(notes)
    kept = [*previous_takes(params), entry]
    dropped, params[PREVIOUS_TAKES] = kept[:-MAX_PREVIOUS_TAKES], kept[-MAX_PREVIOUS_TAKES:]
    files: list[str] = []
    for old in dropped:
        files.extend(_attempt_files(old))
    return files


def restore_previous_take(clip: Any, file_id: str) -> tuple[dict[str, Any], list[str]]:
    """The row's columns and the notes of putting the previous take *file_id* of *clip* back as its take.

    The take that plays joins :data:`PREVIOUS_TAKES` in its place, so a restore is undone the same way. A shot
    being generated again is refused (its attempt would replace the take restored), as is a file that is no
    previous take of the shot. Failed attempts kept beside the take stay, for ``accept_attempt``.
    ``ClipError`` names why it is refused.
    """
    from app.models.video_studio import JOB_PENDING, JOB_READY, JOB_RUNNING
    from app.services.video_studio import frame_qc

    params = dict(getattr(clip, "params", None) or {})
    wanted = str(file_id).strip()
    taken = previous_takes(params)
    entry = next((e for e in taken if str(e.get("file_id")) == wanted), None)
    if entry is None:
        known = ", ".join(f"`{e['file_id']}`" for e in taken) or "none"
        raise ClipError(f"`{wanted}` is no previous take of this shot: restore_take takes one of its previous takes ({known}).")
    if clip.status in (JOB_PENDING, JOB_RUNNING):
        raise ClipError("This shot is being generated again: wait for the attempt to end, then restore the take.")
    rest = [e for e in taken if e is not entry]
    params[PREVIOUS_TAKES] = rest
    kept = params.get(KEPT_TAKE) if isinstance(params.get(KEPT_TAKE), dict) else {}
    playing = {key: getattr(clip, key, None) for key in TAKE_MEDIA}
    if playing.get("file_id"):
        made = {
            **{key: kept[key] for key in ("prompt", "model", "made_at") if kept.get(key) is not None},
            "frame_qc": params.get(frame_qc.QC_KEY),
            "notes": params.get("notes"),
        }
        if not kept:
            made.update(prompt=getattr(clip, "prompt", None), model=getattr(clip, "model", None))
            finished = getattr(clip, "finished_at", None)
            made["made_at"] = finished.isoformat() if finished else None
        # Never past the cap here: the take put back left a place.
        keep_previous_take(params, playing, made)
    params.pop(frame_qc.QC_KEY, None)
    if isinstance(entry.get("frame_qc"), dict):
        params[frame_qc.QC_KEY] = entry["frame_qc"]
    at = datetime.now(UTC).isoformat()
    note = f"Restored ({at[:16]}Z): the take `{wanted}` it played before was put back as the shot's take."
    params["notes"] = [*(entry.get("notes") or []), note]
    if kept:
        params[KEPT_TAKE] = {
            **{key: value for key, value in kept.items() if key not in ("prompt", "model", "made_at")},
            **{key: entry[key] for key in ("prompt", "model", "made_at") if entry.get(key) is not None},
        }
    media = {key: entry.get(key) for key in TAKE_MEDIA if entry.get(key) is not None}
    columns = {**media, "status": JOB_READY, "error": None, "progress": 100, "stage": None, "params": params}
    return columns, [note]


def _kept_start(params: dict) -> dict[str, Any]:
    """The ``reference_*`` arguments that name a shot's start frame as it was asked; ``{}`` for none."""
    source = params.get("reference_video")
    if isinstance(source, dict) and source.get("id"):
        kept: dict[str, Any] = {_SOURCE_ARGUMENT.get(source.get("kind"), "reference_file_id"): str(source["id"])}
        if source.get("step") is not None:
            kept["reference_at_step"] = source["step"]
        elif source.get("at_seconds") is not None:
            kept["reference_at_seconds"] = source["at_seconds"]
        if source.get("crop"):
            kept["crop"] = source["crop"]
        return kept
    if params.get("reference_file_id"):
        return {"reference_file_id": str(params["reference_file_id"])}
    return {}


def _same_start(asked: dict, kept: dict) -> bool:
    """Whether *asked* names the very start *kept* is (a file handle, a moment written another way).

    A moment is compared at the precision a start is stored with (``_video_source``:
    the millisecond): ``2.0004`` names the frame kept at ``2.0``.
    """
    from app.services.builtin_tools._shared import _file_handle_key

    def plain(start: dict) -> dict:
        read: dict[str, Any] = {}
        for key, value in start.items():
            if key in START_SOURCES:
                read[key] = _file_handle_key(str(value)).strip()
            elif key == "reference_at_seconds":
                number = _number(value, key)
                read[key] = None if number is None else round(number, 3)
            elif key == "reference_at_step":
                read[key] = _number(value, key)
            else:
                read[key] = value
        return read

    return bool(kept) and plain(asked) == plain(kept)


def _start_asked(params: dict, changes: dict) -> dict[str, Any]:
    """The ``reference_*`` arguments a shot generated again asks its first frame with; ``{}`` when none.

    A source named (an image, a recording, a shot, a video file) is a new
    start; a moment, a step or ``crop`` alone moves the frame of the video the
    shot started from (an image it started from has no other frame, refused
    as for a new shot).
    """
    asked = {key: changes[key] for key in START_FIELDS if changes.get(key) not in (None, "")}
    if not asked or any(key in asked for key in START_SOURCES):
        return asked
    kept = _kept_start(params)
    if "reference_at_seconds" in asked or "reference_at_step" in asked:
        kept = {key: value for key, value in kept.items() if key not in ("reference_at_seconds", "reference_at_step")}
    return {**kept, **asked}


def _retry_start(
    params: dict, changes: dict, *, references_asked: bool, limits: dict | None, model: str
) -> tuple[dict[str, Any], bool, str | None]:
    """How a shot generated again starts: ``(asked, keep, note)``.

    *asked* is a new start or the kept video at another moment
    (:func:`_start_asked`); *keep* says the kept start frame stays - also when
    the call names the very start the shot has, whose kept frame is used again.
    ``start_frame: false`` drops it. So do reference images the call names
    for a model that takes them but no start frame beside them (*limits*, what
    it declares), unless the call says ``start_frame`` or names a start:
    *note* says so. Nothing is deleted - the kept frame stays a file of the
    conversation.
    """
    flag = changes.get("start_frame")
    asked = _start_asked(params, changes)
    named = bool(asked)
    had = bool(params.get("reference_file_id") or params.get("reference_video"))
    if flag is False:
        if asked:
            raise ClipError(
                "start_frame: false drops the shot's start frame: name no reference_file_id, reference_recording_id, "
                "reference_clip_id, reference_at_seconds, reference_at_step or crop with it."
            )
        return {}, False, "Start frame dropped (start_frame: false)." if had else None
    if asked and _same_start(asked, _kept_start(params)):
        asked = {}
    if asked:
        return asked, False, None
    if flag is True and not had:
        raise ClipError(
            "This shot has no start frame to keep: name one with reference_file_id, reference_recording_id or "
            "reference_clip_id."
        )
    alone = (
        limits is not None
        and int(limits.get("reference_images") or 0) > 0
        and not limits.get("reference_images_with_start_frame")
    )
    if had and flag is None and not named and references_asked and alone:
        return {}, False, f"Start frame dropped: {model} takes reference images alone."
    return {}, had, None


def _drops_references(changes: dict) -> bool:
    """``start_frame: true`` with a ``reference_*`` and no ``reference_image_file_ids``: the start frame alone."""
    return (
        changes.get("start_frame") is True
        and changes.get("reference_image_file_ids") is None
        and any(changes.get(key) not in (None, "") for key in START_FIELDS if key.startswith("reference_"))
    )


def _references_of(params: dict, changes: dict) -> tuple[list[str], bool]:
    """The reference images a shot generated again is asked with: the call's, else its own; and whether the call named some.

    None while it keeps ingredients (:func:`_ingredients_of_retry`), which
    decide their own path; the pictures of ingredients it no longer keeps are
    not its reference images. None either when the call asks its start frame
    alone (:func:`_drops_references`): the way back from reference images to
    a picture the shot continues.
    """
    asked = changes.get("reference_image_file_ids")
    if _named(_ingredients_of_retry(params, changes)) or _drops_references(changes):
        return [], False
    own = None if params.get(INGREDIENTS) else params.get(REFERENCE_IMAGES)
    return _reference_ids(own if asked is None else asked), bool(_reference_ids(asked))


def _ingredients_of_retry(params: dict, changes: dict) -> Any:
    """The ingredients a shot generated again is asked with: the call's, else its own unless it names reference images."""
    asked, references = changes.get("ingredients"), changes.get("reference_image_file_ids")
    if _named(asked) and _named(references):
        raise ClipError("Give ingredients or reference_image_file_ids, not both: an ingredient is a reference image with a role.")
    if asked is not None:
        return asked
    return None if _named(references) else params.get(INGREDIENTS)


def _wanted_model(params: dict, changes: dict) -> tuple[str | None, object]:
    """``(model, provider_id)`` a shot generated again is asked of: the call's, else the shot's own provider."""
    model = (changes.get("model") or "").strip() or None
    return model, changes.get("provider_id") or (None if model else params.get("provider_id"))


def _configured_restart(
    params: dict, changes: dict, *, default: str | None, fallbacks: Sequence[str]
) -> tuple[str | None, object, bool]:
    """``(model, provider_id, configured)`` a shot generated again is asked of under the platform's configuration.

    With a configured default video model the shot is made again on it
    (``(None, None, True)``: the platform's choice, whatever model made it
    first) unless the call names a configured video fallback model; any other
    model is refused (:func:`configured_model`). Without one, :func:`_wanted_model`.
    """
    if default is None:
        model, provider_id = _wanted_model(params, changes)
        return model, provider_id, False
    model = configured_model(changes.get("model"), default=default, fallbacks=fallbacks)
    return model, (changes.get("provider_id") if model else None), True


def _own_provider(params: dict, wanted_provider: object) -> list[tuple[object, str]]:
    """What a shot generated again with another model, its provider not named, prefers when several providers offer
    that model (:func:`resolve`): the provider the shot was made on. Empty when its provider is named."""
    own = params.get("provider_id")
    return [(own, OWN_SHOT_CHOICE)] if own and not wanted_provider else []


async def check_restart(db: Any, clip: Any, *, user_id: object = None, **changes: Any) -> None:
    """What a shot generated again asks, checked before its row is locked or its model resolved.

    Its start frame arguments, and reference images with a start frame against
    what the model declares (``router.declared_video_reference_limits``), so
    a batch of retries the model cannot take is refused at once, naming the
    model and the ways out; :func:`plan_restart` checks again once resolved.
    A provider *user_id* may not use is not read here: the resolution
    refuses it with the access error.
    """
    params = dict(clip.params or {})
    ids, named = _references_of(params, changes)
    asked, keep, _note = _retry_start(params, changes, references_asked=named, limits=None, model=clip.model)
    if not ids or not (asked or keep):
        return
    from app.llm.router import declared_video_reference_limits

    default_model, configured_fallbacks = await configured_models(db)
    wanted_model, wanted_provider, configured = _configured_restart(
        params, changes, default=default_model, fallbacks=configured_fallbacks
    )
    declared = await declared_video_reference_limits(
        db,
        provider_id=wanted_provider,
        model=wanted_model or (None if configured else clip.model),
        user_id=user_id,
        prefer=[] if configured else _own_provider(params, wanted_provider),
    )
    if declared is None:
        return
    model, limits = declared
    asked, keep, _note = _retry_start(params, changes, references_asked=named, limits=limits, model=model)
    check_reference_combination(limits, model, references=len(ids), start_frame=bool(asked) or keep, again=True)


async def plan_restart(db: Any, user: Any, clip: Any, *, tried: list[dict] | None = None, **changes: Any) -> ClipPlan:
    """A shot generated again as it is to be asked, every check passed, or :class:`ClipError`; the row unchanged.

    *tried* is the chain of fallback models whose takes failed, kept
    when the worker falls back to the next one; any other restart starts a
    new chain. One model per round: the shot is made again on its own
    model unless the call names another (``model``), and falls back only to
    the ``fallback_models`` this call names - a chain an earlier call stored
    is the worker's to finish, never inherited by a new call. A shot whose
    model can no longer be used is refused, never handed to another.

    *changes* (``RETRY_FIELDS``) replace the prompt, the model, the settings,
    the reference images or the start frame; what they leave out stays as
    the shot was asked. :func:`restart_clip` applies it under the row's lock;
    ``generate_video_clip(validate_only=true)`` answers it. The start frame
    kept, dropped, or started anew (:func:`_retry_start`), and the reference
    images or the ingredients - the call's or the shot's own, the same files
    sent again - are checked against the model it is generated with now, the
    ingredients' path decided again. The art direction is that of the
    video or template *changes* name (``video_id``, ``template_id``,
    ``freestyle``), else of the template the shot was made for
    (:func:`_kept_direction`).
    """
    from app.llm.router import video_reference_limits
    from app.models.video_studio import JOB_PENDING, JOB_RUNNING
    from app.services.video_studio import frame_qc, insert_keep_out, kept_regions, provider_waits, text_drift
    from app.services.video_studio import stray_screens as stray
    from app.services.video_studio.content_drift import ACCEPT_MINOR_KEY
    from app.services.video_studio.text_drift import LETTERING_KEY

    if clip.status in (JOB_PENDING, JOB_RUNNING):
        raise ClipError("This shot is still being made: wait for it (get_video_jobs), then generate it again if needed.")
    params = dict(clip.params or {})
    failed_before = failed_regeneration(clip)
    plays = playable(clip)
    if plays and not changes.get("discard"):
        # Kept until the new take is ready and passed its checks: every scene using it keeps playing it.
        keep_take(params, clip)
        previous_take = "kept"
    else:
        # A failed take (one failed after its video was made: a drift), or a ready one discarded: its files go
        # with the new generation, and so do the attempts kept beside it.
        params[REPLACED_MEDIA] = list(
            dict.fromkeys(
                [
                    *(params.get(REPLACED_MEDIA) or []),
                    *(str(fid) for fid in (clip.file_id, clip.poster_file_id) if fid),
                    *frame_qc.frame_file_ids(params),
                    *_beside_take(params),
                ]
            )
        )
        for key in (frame_qc.QC_KEY, KEPT_TAKE, ATTEMPTS, ATTEMPT_MEDIA, PREVIOUS_TAKES):
            params.pop(key, None)
        if not params[REPLACED_MEDIA]:
            params.pop(REPLACED_MEDIA)
        previous_take = "discarded" if plays else None
    prompt = (changes.get("prompt") or clip.prompt or "").strip()
    if not prompt:
        raise ClipError("A shot needs a prompt describing it (subject, action, camera, light, style).")
    if changes.get("video_id") or changes.get("template_id") or changes.get("freestyle"):
        direction = await resolve_direction(
            db,
            user,
            video_id=changes.get("video_id"),
            template_id=changes.get("template_id"),
            freestyle=bool(changes.get("freestyle")),
        )
    else:
        direction = await _kept_direction(db, user, params)
    _record_direction(params, direction)
    default_model, configured_fallbacks = await configured_models(db)
    if tried:
        # The worker falling back along a chain already held to the configuration: the next model as it names it.
        wanted_model, wanted_provider = _wanted_model(params, changes)
        configured = False
    else:
        wanted_model, wanted_provider, configured = _configured_restart(
            params, changes, default=default_model, fallbacks=configured_fallbacks
        )
    chosen: list[str] = []
    # Another model named alone that several providers offer: the shot's own provider, then its video's.
    prefer = (
        []
        if wanted_provider or (configured and not wanted_model)
        else [*_own_provider(params, None), *await video_providers(db, user, changes.get("video_id"), wanted_model)]
    )
    try:
        provider, chosen_model, config = await resolve(
            db,
            user.id,
            model=wanted_model or (None if configured else clip.model),
            provider_id=wanted_provider,
            prefer=prefer,
            notes=chosen,
        )
    except ClipError as exc:
        if wanted_model or changes.get("provider_id") or configured:
            raise
        # The shot's own model is gone (retired, its provider removed): said, never handed to another.
        raise ClipError(
            f"The shot's model {clip.model} can no longer make it ({str(exc).rstrip('. ')}): name the model to make "
            "it with (model)",
            about_model=True,
            not_served=exc.not_served,
        ) from None
    if configured and clip.model and chosen_model != clip.model:
        chosen.append(
            f"Made again on {chosen_model}, "
            + ("the configured default video model" if chosen_model == default_model else "a configured video fallback model")
            + f": its earlier take was made on {clip.model}."
        )
    # The same reference images as before, unless the call names others: checked against the model now.
    ids, named = _references_of(params, changes)
    asked, keep, note = _retry_start(
        params,
        changes,
        references_asked=named,
        limits=video_reference_limits(provider, chosen_model) if ids else None,
        model=chosen_model,
    )
    notes = [*chosen, *([note] if note else [])]
    if _drops_references(changes) and params.get(REFERENCE_IMAGES) and not params.get(INGREDIENTS):
        notes.append("Reference images dropped: start_frame: true with a reference_* asks the start frame alone.")
    request = build_request(
        model=chosen_model,
        prompt=prompt,
        aspect_ratio=changes.get("aspect_ratio") or params.get("aspect_ratio", "16:9"),
        duration_seconds=int(changes.get("duration_seconds") or params.get("duration_seconds") or 8),
        resolution=changes.get("resolution") or params.get("resolution", "1080p"),
        negative_prompt=changes.get("negative_prompt") or params.get("negative_prompt"),
        sound=changes["sound"] if changes.get("sound") is not None else params.get("sound"),
    )
    await refuse_unpaid(provider, config, request)
    started = bool(asked) or keep
    # How its pictures are fitted to the model: asked anew, else the shot's own.
    fit = reference_fit_choice(changes.get("reference_fit")) or params.get(REFERENCE_FIT)
    if fit == FIT_PAD:
        params[REFERENCE_FIT] = FIT_PAD
    else:
        params.pop(REFERENCE_FIT, None)
    references = await resolve_reference_images(
        db, user, ids, provider=provider, model=chosen_model, start_frame=started, again=True, fit=fit, notes=notes
    )
    if references:
        params[REFERENCE_IMAGES] = references
    else:
        params.pop(REFERENCE_IMAGES, None)
    # The same ingredients as before, unless the call names others: their path decided by the model now.
    kept, path = await resolve_ingredients(
        db,
        user,
        _ingredients_of_retry(params, changes),
        provider=provider,
        model=chosen_model,
        start_frame=started,
        fit=fit,
        notes=notes,
    )
    keep_ingredients(params, kept, path)
    # The video's references it is held to: asked anew, else the shot's own, their path decided by the model now.
    own = params.get(CONSISTENCY) if isinstance(params.get(CONSISTENCY), dict) else None
    asked_consistency = changes.get("consistency")
    if asked_consistency is None and own is not None:
        asked_consistency = [str(item.get("id")) for item in own.get("references") or []]
    held = await resolve_consistency(
        db,
        user,
        asked_consistency,
        video_id=changes.get("video_id") or (own or {}).get("video_id"),
        provider=provider,
        model=chosen_model,
        start_frame=started,
        reference_images=len(references) + (len(kept) if path == PATH_NATIVE else 0),
    )
    keep_consistency(params, held)
    if changes.get("min_reference_similarity") is not None:
        threshold = reference_similarity_threshold(changes["min_reference_similarity"], start_frame=started)
    else:
        threshold = params.get(MIN_REFERENCE_SIMILARITY)
        if threshold is not None and not started:
            threshold = None
            notes.append("min_reference_similarity dropped: the shot has no start frame to compare its first frame with.")
    if threshold is None:
        params.pop(MIN_REFERENCE_SIMILARITY, None)
    else:
        params[MIN_REFERENCE_SIMILARITY] = threshold
    if changes.get("max_camera_motion") is not None:
        tolerance = camera_motion_tolerance(changes["max_camera_motion"])
        if tolerance is None:
            params.pop(frame_qc.MAX_CAMERA_MOTION, None)
        else:
            params[frame_qc.MAX_CAMERA_MOTION] = tolerance
    # A locked-off camera the model does not hold is said before the take is paid for.
    notes.extend(
        await locked_camera(
            db, user, (provider, chosen_model, config), tolerance=params.get(frame_qc.MAX_CAMERA_MOTION), start_frame=started
        )
    )
    # A term of the negative prompt the last takes kept drawing: asked in the prompt itself this time.
    insisted = insistent_terms(params, request.negative_prompt, attempt=_attempt(params) + 1)
    if insisted:
        guidance = lowered_guidance(provider, chosen_model, params)
        params[INSISTED] = {"terms": insisted, "model": chosen_model, **({"guidance": guidance} if guidance is not None else {})}
        notes.append(insisted_note(insisted, guidance, chosen_model))
    else:
        params.pop(INSISTED, None)
    # Regions kept from the start picture: asked anew, else the shot's own - redrawn on the new take.
    regions = kept_regions_asked(changes.get("keep_regions"), start_frame=started)
    if regions is None:
        regions = kept_regions.boxes_of(params)
        if regions and not started:
            regions = []
            notes.append("keep_regions dropped: the shot has no start picture to keep them from.")
    if regions:
        params[kept_regions.KEY] = {"boxes": regions}
    else:
        params.pop(kept_regions.KEY, None)
    # Whether it carries a screen insert: asked anew, else the shot's own; a screen asked anew is one.
    asked_insert = insert_choice(changes.get("insert"))
    insert = asked_insert if asked_insert is not None else params.get(INSERT)
    if insert is False and (changes.get("screen") is not None or isinstance(params.get(SCREEN), dict)):
        if asked_insert is False:
            raise ClipError(
                "insert: false says the shot carries no screen insert, and it replaces a screen (screen): the "
                "screenshot would never be drawn - generate a new shot without screen instead."
            )
        insert = None
    # The stray screens' cleanup: asked anew, else the shot's own, checked against what it is asked now.
    cleaning = changes["stray_screens"] if changes.get("stray_screens") is not None else params.get(stray.KEY)
    if isinstance(cleaning, dict) and cleaning.get("mode") == stray.FLATTEN:
        cleaning = stray.FLATTEN
    if isinstance(cleaning, dict):
        cleaning = {key: cleaning[key] for key in ("file_id", "fit", "light") if key in cleaning}
    try:
        if insert is False:
            cleanup = None
            if cleaning not in (None, "", "fail"):
                notes.append("stray_screens dropped: insert: false says the shot carries no screen to clean.")
        else:
            cleanup = await resolve_stray_screens(
                db,
                user,
                cleaning,
                negative_prompt=request.negative_prompt,
                content_check=(
                    changes["content_check"] if changes.get("content_check") is not None else params.get("content_check", True)
                )
                is not False,
            )
    except ClipError as exc:
        if changes.get("stray_screens") is not None:
            raise
        cleanup = None
        notes.append(f"stray_screens dropped: {exc}")
    if cleanup:
        params[stray.KEY] = cleanup
    else:
        params.pop(stray.KEY, None)
    if changes.get("stabilise") is not None:
        params[STABILISE] = stabilise_choice(changes["stabilise"])
    if not tried and (changes.get("fallback_models") is not None or default_model is not None):
        # The call's chain, held to the configuration; none named, the configured video fallback models.
        fallbacks, said = await resolve_fallbacks(
            db,
            user,
            changes.get("fallback_models"),
            model=chosen_model,
            params=params,
            again=True,
            configured=(default_model, configured_fallbacks),
        )
        notes.extend(said)
        if fallbacks:
            params[FALLBACK_MODELS] = fallbacks
        else:
            params.pop(FALLBACK_MODELS, None)
    elif not tried:
        # A new call naming no fallback has none: the chain an earlier call named is not this one's.
        params.pop(FALLBACK_MODELS, None)
    if tried:
        params[FALLBACK_TRIED] = list(tried)
    else:
        params.pop(FALLBACK_TRIED, None)
    if asked:
        # A new start, checked as a new shot's is: one source, ready, a frame of the shot's shape.
        reference = await resolve_reference(
            db, user, aspect_ratio=request.aspect_ratio, **{START_FIELDS[key]: value for key, value in asked.items()}
        )
        params.pop("reference_video", None)
        params["reference_file_id"] = reference.get("reference_file_id")
        if "reference_video" in reference:
            params["reference_video"] = reference["reference_video"]
    elif not keep:
        params.pop("reference_video", None)
        params["reference_file_id"] = None
    # Counted once the start it is made from is known: a new one starts the count again.
    count_regeneration(params, prompt=prompt, changes=changes)
    attempt = int(params.get("attempt") or 0) + 1
    params.update(
        {
            "provider_id": str(config.id),
            "aspect_ratio": request.aspect_ratio,
            "duration_seconds": request.duration_seconds,
            "resolution": request.resolution,
            "negative_prompt": request.negative_prompt,
            "attempt": attempt,
            "previous_error": clip.error or (failed_before or {}).get("reason"),
        }
    )
    if request.sound is not None:
        params["sound"] = request.sound
    if changes.get("sound") is not None:
        params.pop(SILENT_FOR_VIDEO, None)
    if changes.get("text_check") is not None:
        params["text_check"] = bool(changes["text_check"])
    if changes.get("content_check") is not None:
        params["content_check"] = bool(changes["content_check"])
    if changes.get("accept_minor_drift") is not None:
        params[ACCEPT_MINOR_KEY] = bool(changes["accept_minor_drift"])
    if changes.get("ingredients_quality") is not None:
        params.pop("ingredients_quality", None)
        params.update(ingredients_quality_asked(changes["ingredients_quality"]))
    asked_screen = changes.get("screen")
    moved = bool(changes.get("aspect_ratio")) or bool(asked) or not keep
    if asked_screen is not None or (isinstance(params.get(SCREEN), dict) and moved):
        # A new quad or screenshot, or the stored one checked against the shot's new shape or new start
        # frame: its quad is in the start picture's pixels.
        params[SCREEN] = await resolve_screen(
            db,
            user,
            asked_screen if asked_screen is not None else screen_again(params[SCREEN]),
            reference={k: params.get(k) for k in ("reference_file_id", "reference_video")},
            aspect_ratio=request.aspect_ratio,
        )
    # The screen its start picture shows, measured again on the start it is made from now, when the shot carries a
    # screen insert: a surface kept from before by a shot carrying none is dropped.
    surface, said = await lint_surface(
        db,
        user,
        {k: params.get(k) for k in ("reference_file_id", "reference_video")},
        params.get(SCREEN) if isinstance(params.get(SCREEN), dict) else None,
        insert=insert,
        clip_id=clip.id,
    )
    notes.extend(said)
    params.pop(SURFACE, None)
    if surface:
        params[SURFACE] = surface
    if insert is None:
        params.pop(INSERT, None)
    else:
        params[INSERT] = insert
    # The lettering its start picture shows, read again on the start it is made from now: gibberish refused.
    lettering, said = await lint_lettering(
        db,
        user,
        {k: params.get(k) for k in ("reference_file_id", "reference_video")},
        text_check=bool(params.get("text_check", True)),
        regions=kept_regions.boxes_of(params),
    )
    notes.extend(said)
    params.pop(text_drift.START_LETTERING_KEY, None)
    if lettering:
        params[text_drift.START_LETTERING_KEY] = lettering
    params.pop("notes", None)
    # What the start picture already shows of the negative prompt, read again on the start and negative prompt now.
    notes.extend(await lint_start_shown(user, params))
    # What the start picture already fails of the prompt's subjects, read again on the start and prompt now.
    await lint_start_content(user, params, prompt)
    # The lettering a former take had drawn back: the new take is checked, and rescued, on its own.
    params.pop(LETTERING_KEY, None)
    # A new round: its waits for a busy provider are its own.
    params.pop(provider_waits.KEY, None)
    # What a shot carrying a screen insert is asked of its screen: its rectangle kept clear.
    if said := insert_keep_out.note(params, provider, chosen_model):
        notes.append(said)
    return ClipPlan(
        request=request,
        config=config,
        params=params,
        notes=notes,
        sent=_sent(params, request, provider),
        previous_take=previous_take,
    )


async def restart_clip(
    db: Any, user: Any, clip: Any, *, owed_to: Any = None, tried: list[dict] | None = None, **changes: Any
) -> ClipPlan:
    """Generate a shot again, in place: every video using it keeps it.

    A failed, cancelled or expired shot, or a ready one that came out wrong:
    the same row and id, so no scene changes; *changes*
    (``RETRY_FIELDS``, :func:`plan_restart`) replace the prompt, the model,
    the settings, the reference images or the start frame first. What the
    model cannot take is refused before the row is locked
    (:func:`check_restart`). A ready shot keeps playing its take until the
    new one is ready and passed its checks (:data:`KEPT_TAKE`): an
    attempt that fails leaves it ready on that take, the attempt kept beside
    it; ``discard: true`` drops the take at once instead (deleted when the
    new generation ends). Only a shot still being made is refused. The row
    is locked before its status is read, so two requests at once (the editor
    and the assistant) start one attempt, not two. *owed_to* is the
    conversation of the chat turn asking (``jobs.enqueue``). A provider that
    cools (``provider_cooling``) has the shot queued for when it cools, its
    row saying why. Returns the plan applied (its provider label, what it
    changed by itself); raises :class:`ClipError`. Flushed and enqueued, not
    committed.
    """
    from app.models.video_studio import JOB_PENDING
    from app.services.video_studio import jobs, provider_cooling

    await check_restart(db, clip, user_id=user.id, **changes)
    await db.refresh(clip, with_for_update=True)
    plan = await plan_restart(db, user, clip, tried=tried, **changes)
    request, config = plan.request, plan.config
    attempt = int(plan.params["attempt"])
    clip.prompt = request.prompt
    clip.model = request.model
    clip.provider = config.provider
    clip.params = plan.params
    clip.status = JOB_PENDING
    clip.progress = 0
    clip.error = None
    clip.finished_at = None
    # Queued again: nothing the last attempt's worker or provider said still stands.
    clip.stage = clip.provider_state = None
    clip.progress_at = clip.provider_eta_at = None
    # A new generation: nothing of the last one is resumed.
    clip.provider_job_id = None
    if not keeps_take(plan.params):
        # Nothing plays meanwhile: a failed take, or a ready one discarded.
        clip.file_id = None
        clip.poster_file_id = None
        clip.expired_at = None
    await db.flush()
    cooling = await provider_cooling.cooling_of(config.id, model=request.model)
    when = {"not_before": cooling.until} if cooling is not None else {}
    await jobs.enqueue(db, "clip", clip, generation=attempt, owed_to=owed_to, **when)
    if cooling is not None:
        provider_cooling.hold(clip, cooling)
    return plan
