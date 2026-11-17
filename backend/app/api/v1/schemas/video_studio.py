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

"""Request and response bodies of the Video Studio API."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class VideoSpecWrite(_Body):
    spec: dict[str, Any]


class VideoOperationsWrite(_Body):
    operations: list[dict[str, Any]] = Field(min_length=1, max_length=40)


class VideoProjectUpdate(_Body):
    """``PATCH /projects/{id}``: who may open the video, a deck's sharing fields with PATCH semantics.

    Written through ``presentation_sharing.apply_sharing``, the one sharing
    write decks and videos share (``share_principals.SharingChange`` names the
    same fields).
    """

    is_company_managed: bool | None = None
    shared_group_ids: list[str] | None = None
    shared_group_permissions: dict[str, str] | None = None
    shared_user_ids: list[str] | None = None
    shared_user_permissions: dict[str, str] | None = None
    access_restricted: bool | None = None


class VideoProjectCreate(_Body):
    spec: dict[str, Any]


class VideoPreviewRequest(_Body):
    #: The editor's draft; omitted = the video as stored.
    spec: dict[str, Any] | None = None


class ClipIngredientSubject(_Body):
    """The subject a logo sits on: a picture of it wearing the logo, and what to call it."""

    file_id: str = Field(min_length=1, max_length=200)
    label: str | None = Field(None, max_length=80)


class ClipIngredient(_Body):
    """A picture an AI shot keeps - a logo, a product, a character - and what to call it."""

    file_id: str = Field(min_length=1, max_length=200)
    role: Literal["logo", "product", "character"]
    label: str | None = Field(None, max_length=80)
    #: A logo's corners in the start frame, 0-1, where it is drawn back from; unset, it is found there.
    quad: list[Annotated[list[float], Field(min_length=2, max_length=2)]] | None = Field(None, min_length=4, max_length=4)
    #: A logo on a subject the start frame does not show: drawn onto it wherever the take shows it.
    subject: ClipIngredientSubject | None = None


class ClipCreate(_Body):
    """An AI shot asked from the editor: generated, then placed in a video."""

    prompt: str = Field(min_length=1, max_length=4000)
    name: str | None = Field(None, max_length=200)
    model: str | None = Field(None, max_length=120)
    provider_id: str | None = Field(None, max_length=64)
    duration_seconds: int = Field(8, ge=1, le=20)
    aspect_ratio: Literal["16:9", "9:16"] = "16:9"
    resolution: Literal["720p", "1080p"] = "1080p"
    negative_prompt: str | None = Field(None, max_length=2000)
    #: The first frame: an image or a video file, a recording, or a shot
    #: continued - a video's last frame unless a moment or a recording's step.
    reference_file_id: str | None = Field(None, max_length=64)
    reference_recording_id: str | None = Field(None, max_length=64)
    reference_clip_id: str | None = Field(None, max_length=64)
    reference_at_seconds: float | None = Field(None, ge=0)
    reference_at_step: int | None = Field(None, ge=0)
    #: ``False`` asks a silent shot; unset, the model's own choice.
    sound: bool | None = None
    #: ``False`` skips the text check of a shot made from a picture (one meant to change its words).
    text_check: bool = True
    #: A video's frame of another shape than the shot's: its middle, cut to the shot's ratio.
    crop: Literal["center"] | None = None
    #: Pictures whose subjects the shot keeps, for a model that takes them.
    reference_image_file_ids: list[str] | None = Field(None, min_length=1, max_length=3)
    #: Pictures the shot keeps with what they are: through the model's own references, else named in the prompt.
    ingredients: list[ClipIngredient] | None = Field(None, min_length=1, max_length=3)
    #: Fails a take whose first frame's similarity with its start frame is under it.
    min_reference_similarity: float | None = Field(None, ge=0.0, le=1.0)
    #: Fails a take whose last frame changed more than this from its first (1 - SSIM).
    max_camera_motion: float | None = Field(None, ge=0.0, le=1.0)
    #: Locks a drifting camera off once the take is made; unset, when its camera is asked still.
    stabilise: bool | None = None
    #: Models tried in order when a take fails a check of its frames.
    fallback_models: list[str] | None = Field(None, max_length=3)
    #: ``False`` skips the check of the subjects the prompt names.
    content_check: bool = True
    #: ``False`` fails the shot on a content drift seen only up close too.
    accept_minor_drift: bool = True
    #: Screen replacement: ``{file_id, quad?, fit?, light?}`` - the screenshot drawn into the screen of the start
    #: picture; without ``quad``, the screen is found in it (``screens.find_screen``).
    screen: dict[str, Any] | None = None
    #: Boxes ``[x, y, width, height]`` (shares of the start picture) drawn back as they are in every frame.
    keep_regions: list[list[float]] | None = Field(None, max_length=4)
    #: ``fail`` (default), ``flatten`` or ``{file_id, fit?, light?}``: the screens a take shows against its
    #: negative prompt, cleaned instead of failing it.
    stray_screens: str | dict[str, Any] | None = None
    #: Whether the shot is footage a screen insert is drawn on: ``True`` checks its start picture's screen,
    #: ``False`` checks no screen and drops ``stray_screens``; unset, only a screen replacement's is checked.
    insert: bool | None = None
    #: ``integrated`` (default): a logo drawn onto the take that reads as a flat sticker fails it as ``logo not
    #: integrated``; ``flat`` accepts it, recorded (``clips.ingredients_quality_asked``).
    ingredients_quality: Literal["integrated", "flat"] | None = None
    #: A reference image or ingredient outside the shape the model takes: ``refuse`` (default) or ``pad``.
    reference_fit: Literal["refuse", "pad"] | None = None
    #: The video the shot joins (a clip scene at ``index``, else at the end).
    project_id: str | None = Field(None, max_length=64)
    index: int | None = Field(None, ge=0, le=500)
    #: No ``project_id``: start a new video made of this shot.
    new_video: bool = False


class PictureCompose(_Body):
    """``POST /pictures/compose``: real pictures inside a picture (``pictures.compose_files``)."""

    base_file_id: str = Field(min_length=1, max_length=64)
    #: 1-4 layers: ``{file_id, x, y, width, height?}`` or ``{file_id, quad: [[x, y] x 4]}``, ``opacity``, ``fit``.
    layers: list[dict[str, Any]] = Field(min_length=1, max_length=4)
    name: str | None = Field(None, max_length=120)


class ComposedPicture(BaseModel):
    file_id: str
    url: str
    width: int
    height: int


class ClipStartFrameLoading(BaseModel):
    """A recording's page still loading at the frame asked, and when it settles (``None``: not soon)."""

    at_seconds: float
    settled_at_seconds: float | None


class ClipStartFramePreview(BaseModel):
    """``GET /clips/start-frame``: the frame a shot would open on, a file of the caller's."""

    file_id: str
    url: str
    at_seconds: float | None
    width: int | None
    height: int | None
    note: str
    loading: ClipStartFrameLoading | None


class ClipRetry(_Body):
    """An AI shot generated again in place (failed, expired, or ready but wrong); each field set replaces the one it was asked with."""

    prompt: str | None = Field(None, min_length=1, max_length=4000)
    model: str | None = Field(None, max_length=120)
    provider_id: str | None = Field(None, max_length=64)
    duration_seconds: int | None = Field(None, ge=1, le=20)
    aspect_ratio: Literal["16:9", "9:16"] | None = None
    resolution: Literal["720p", "1080p"] | None = None
    negative_prompt: str | None = Field(None, max_length=2000)
    sound: bool | None = None
    text_check: bool | None = None
    content_check: bool | None = None
    accept_minor_drift: bool | None = None
    #: Replaces the shot's reference images (``[]`` drops them); on a model that takes no start
    #: frame beside them, drops the shot's start frame unless ``start_frame`` says otherwise.
    reference_image_file_ids: list[str] | None = Field(None, max_length=3)
    #: A new start, resolved as ``POST /clips`` resolves it; a moment, a step or ``crop``
    #: alone moves the frame of the video the shot started from.
    reference_file_id: str | None = Field(None, max_length=64)
    reference_recording_id: str | None = Field(None, max_length=64)
    reference_clip_id: str | None = Field(None, max_length=64)
    reference_at_seconds: float | None = Field(None, ge=0)
    reference_at_step: int | None = Field(None, ge=0)
    crop: Literal["center"] | None = None
    #: ``False`` drops the shot's start frame (its file stays in the conversation); ``True`` keeps it.
    start_frame: bool | None = None
    #: Replaces the shot's ingredients (``[]`` drops them).
    ingredients: list[ClipIngredient] | None = Field(None, max_length=3)
    #: Replaces its similarity threshold (0 drops it).
    min_reference_similarity: float | None = Field(None, ge=0.0, le=1.0)
    #: Replaces the video's references the shot is held to (``[]`` drops them); needs ``video_id``.
    consistency: list[str] | None = Field(None, max_length=3)
    #: Replaces how much its camera may move (1 drops it).
    max_camera_motion: float | None = Field(None, ge=0.0, le=1.0)
    #: Replaces whether a drifting take is stabilised.
    stabilise: bool | None = None
    #: Replaces the models tried when a take fails its checks (``[]`` drops them).
    fallback_models: list[str] | None = Field(None, max_length=3)
    #: The video (or the template) the shot is generated again for: under its art direction;
    #: ``freestyle`` for none. Unset, the template the shot was made for.
    video_id: str | None = Field(None, max_length=64)
    template_id: str | None = Field(None, max_length=64)
    freestyle: bool | None = None
    #: Replaces the shot's screen replacement (a new quad or screenshot).
    screen: dict[str, Any] | None = None
    #: Replaces the regions kept from the start picture (``[]`` drops them) and the stray screens' cleanup
    #: (``fail`` drops it).
    keep_regions: list[list[float]] | None = Field(None, max_length=4)
    stray_screens: str | dict[str, Any] | None = None
    #: Replaces whether the shot is footage a screen insert is drawn on.
    insert: bool | None = None
    #: Replaces whether a logo drawn flat is accepted: ``flat`` or ``integrated``.
    ingredients_quality: Literal["integrated", "flat"] | None = None
    #: Replaces how a reference image outside the model's accepted shape is fitted: ``refuse`` or ``pad``.
    reference_fit: Literal["refuse", "pad"] | None = None
    #: A ready shot keeps playing its take until the new one is ready and passed its checks;
    #: ``True`` drops that take now instead.
    discard: bool | None = None


class MusicCreate(_Body):
    """An AI track asked from the editor: composed, then the video's music."""

    prompt: str = Field(min_length=1, max_length=2000)
    name: str | None = Field(None, max_length=200)
    model: str | None = Field(None, max_length=120)
    provider_id: str | None = Field(None, max_length=64)
    #: The length asked; with ``project_id`` and none, the video's own.
    duration_seconds: int | None = Field(None, ge=5, le=600)
    instrumental: bool = True
    negative_prompt: str | None = Field(None, max_length=500)
    #: The video whose music it becomes (its levels kept).
    project_id: str | None = Field(None, max_length=64)


class MusicRetry(_Body):
    """A failed AI track generated again in place; each field set replaces the one it was asked with."""

    prompt: str | None = Field(None, min_length=1, max_length=2000)
    model: str | None = Field(None, max_length=120)
    provider_id: str | None = Field(None, max_length=64)
    duration_seconds: int | None = Field(None, ge=5, le=600)
    instrumental: bool | None = None
    negative_prompt: str | None = Field(None, max_length=500)


class MediaAssetUpdate(_Body):
    """A recording, an AI shot or an AI track: renamed, kept out of the retention or not."""

    name: str | None = Field(None, min_length=1, max_length=200)
    keep: bool | None = None


#: The most media a bulk deletion names one by one; past it, the library's filters say which.
MAX_BULK_DELETE_ITEMS = 500


class MediaRef(_Body):
    kind: Literal["take", "clip"]
    id: str = Field(..., min_length=1, max_length=64)


class MediaBulkDelete(_Body):
    """``POST /assets/bulk-delete``: the media in ``items``, or every one ``GET /assets`` lists with the filters."""

    items: list[MediaRef] | None = Field(None, max_length=MAX_BULK_DELETE_ITEMS)
    all_matching_filters: bool = False
    #: The filters of ``GET /assets``, read with ``all_matching_filters`` only.
    kind: Literal["all", "take", "clip"] = "all"
    search: str | None = Field(None, max_length=200)
    usage: Literal["all", "used", "unused"] = "all"
    date_from: datetime | None = None
    date_to: datetime | None = None


class MediaBulkDeleteResult(BaseModel):
    deleted: int
    #: The media named or matched that a video uses: never deleted in bulk, each one forced on its own.
    kept_in_use: int = 0


class RenderRequest(_Body):
    quality: Literal["draft", "standard", "high", "ultra"] = "high"
    #: Render past the quality gate: the override is kept on the render, logged and audited.
    force: bool = False
    #: Cancel the render of an older revision still in progress; without it, the start answers 409
    #: ``render_replace`` with the question to confirm.
    replace: bool = False


class SceneRenderRequest(_Body):
    """A preview of one scene: its id, or its 0-based index in the spec as written."""

    scene: str | int = Field(..., description="The scene's id, or its 0-based index in the spec as written.")
    quality: Literal["draft", "standard"] | None = Field(
        None,
        description=(
            "draft (720p30) or standard (1080p30); unset, 1080p for a scene showing a figure, a recording or a "
            "screen insert and a draft otherwise."
        ),
    )
    #: Preview past the scene's pre-render lint: audited, like a forced render.
    force: bool = False


class RenderUpdate(_Body):
    """``PATCH /renders/{id}``: keep it out of the render retention, or make it the one the video plays."""

    keep: bool | None = None
    current: Literal[True] | None = None


class RenderCleanupRequest(_Body):
    """``POST /projects/{id}/renders/cleanup``: ``delete_video_renders``' arguments (``render_cleanup.Ask``)."""

    render_ids: list[str] | None = Field(default=None, max_length=500)
    select: Literal["all_but_latest_ready", "failed_and_cancelled", "previews", "all"] | None = None
    keep_quality: str | None = Field(default=None, max_length=20)
    #: Only the renders made before this time.
    before: datetime | None = None
    dry_run: bool = False
    #: Also deletes the render the public link plays, and with ``select: all`` the newest ready renders.
    confirm: bool = False


class RenderRetentionWrite(_Body):
    """``PUT /render-retention``: the days a render nothing else keeps is kept after it ended; 0 keeps it."""

    failed_cancelled_days: int | None = Field(default=None, ge=0, le=3650)
    previews_days: int | None = Field(default=None, ge=0, le=3650)
    superseded_ready_days: int | None = Field(default=None, ge=0, le=3650)


class MediaTokenRequest(_Body):
    kind: Literal["take", "clip", "render", "music"]
    id: str
    #: ``start_frame``: what an AI shot opened on. ``first_frame`` / ``mid_frame`` / ``last_frame``:
    #: an AI shot's own frames, or a scene preview's.
    asset: Literal[
        "video", "poster", "captions", "contact_sheet", "audio", "start_frame", "first_frame", "mid_frame", "last_frame"
    ] = "video"
    #: A download: the file is an attachment saved under the video's
    #: title and quality, not played.
    download: bool = False


class MediaTokenResponse(BaseModel):
    url: str
    expires_at: int
    content_type: str


class JobList(BaseModel):
    items: list[dict[str, Any]]


class LibraryPage(BaseModel):
    """A page of a library: its rows, and how many match in all."""

    items: list[dict[str, Any]]
    total: int


class VideoPublicLinkRequest(_Body):
    """``POST /projects/{id}/public-link``: turn the link on, or issue it a new token."""

    regenerate: bool = False


class VideoPublicLinkUpdate(_Body):
    """``PATCH /projects/{id}/public-link``: whether its visitors may download the video."""

    allow_download: bool


class VideoPublicLinkResponse(BaseModel):
    """A video's public link as its managers read it, the deck's shape.

    ``available`` is the admin switch: off, a kept link (``enabled``) plays
    nothing and none can be created. ``path`` is the web app page without the
    base path; ``path`` and ``url`` are ``None`` while the link is off, or when
    ``SECRET_KEY`` no longer decrypts the stored token (regenerate it).
    """

    available: bool
    enabled: bool
    path: str | None = None
    url: str | None = None
    created_at: str | None = None
    created_by_name: str | None = None
    allow_download: bool = False


class VideoChapter(BaseModel):
    """One chapter of a render (``timeline.chapters``): where its scene takes over, in seconds."""

    start: float
    type: str
    title: str | None = None


class PublicVideoResponse(BaseModel):
    """``GET /public/videos/{token}``: what the public player needs, and nothing about who made or may open it."""

    title: str
    rendered: bool
    duration_ms: int | None = None
    width: int | None = None
    height: int | None = None
    fps: int | None = None
    has_poster: bool = False
    has_captions: bool = False
    captions_burned_in: bool = True
    rendered_at: str | None = None
    download: bool = False
    size_bytes: int | None = None
    chapters: list[VideoChapter] = Field(default_factory=list)


class PronunciationLexiconWrite(_Body):
    """``PUT /pronunciation-lexicon``: a new version of the platform's lexicon, validated entry by entry."""

    #: ``[{text, say, ipa?, match?, apply_to?}]`` (``spec.Pronunciation``).
    entries: list[dict[str, Any]] = Field(max_length=500)
    #: What changed, for the version history.
    note: str | None = Field(None, max_length=500)


class PronunciationSpeakRequest(_Body):
    """``POST /pronunciations/speak``: one lexicon term spoken as a video's voice-over says it."""

    #: The term as written.
    text: str = Field(min_length=1, max_length=160)
    #: The editor's draft voice-over (its speech model, voice, style, speed and own entries); omitted = the platform's.
    voiceover: dict[str, Any] | None = None
