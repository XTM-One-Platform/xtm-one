# SPDX-License-Identifier: Apache-2.0

"""Video Studio models: recordings, AI clips, video projects and renders.

Capture and production are separate on purpose:

* a ``VideoTake`` is one scripted recording of the backend browser - its
  mezzanine video, its action log (every target element's box, the cursor
  path) and the browser profile it was signed in with
  (``models/browser_profile.py``). It is an asset: several
  projects can use it, and re-running its script records a fresh take;
* a ``VideoClip`` is one AI-generated clip (OpenAI Sora, Google Veo), also an
  asset; a ``VideoMusic`` one AI-composed music track, the bed a
  video plays under its scenes;
* a ``VideoProject`` is the edit: a validated timeline spec
  (``services/video_studio/spec.py``) that references takes, clips, deck
  slides and images by id. It carries the three-tier visibility column set,
  like a presentation, so a colleague can open, review and re-render it;
* a ``VideoRender`` is one production of a project at one revision and one
  quality preset - the MP4, its poster and its WebVTT captions. Renders are
  kept under a retention (``services/video_studio/render_retention.py``): the newest few, the project's current one and those kept by hand;
* a ``VideoPublicLink`` opens one project's current render to anyone holding
  its URL, without an account, like a deck's public link.

File outputs are ``FileAttachment`` rows referenced by id without a foreign
key: ``file_attachments`` is write-hot, and every reader here handles a file
that is gone. Deleting a take, a clip or a project deletes the files it
produced (``services/video_studio/assets.py``).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy import BigInteger, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.visibility import VisibilityColumns

#: Lifecycle shared by takes, clips and renders.
JOB_PENDING = "pending"
JOB_RUNNING = "running"
JOB_READY = "ready"
JOB_FAILED = "failed"
JOB_CANCELLED = "cancelled"
JOB_STATUSES = (JOB_PENDING, JOB_RUNNING, JOB_READY, JOB_FAILED, JOB_CANCELLED)
JOB_TERMINAL_STATUSES = frozenset({JOB_READY, JOB_FAILED, JOB_CANCELLED})


def _now() -> datetime:
    return datetime.now(UTC)


class _RetainedMedia:
    """The retention of a recording's or an AI shot's own media.

    The files are the heavy part (a mezzanine video per recording). Once no
    video uses them (never while one does), the retention sweep
    (``services/video_studio/retention.py``) deletes them
    ``VIDEO_STUDIO_MEDIA_RETENTION_DAYS`` after the last video using them
    stopped, or ``VIDEO_STUDIO_UNUSED_MEDIA_RETENTION_DAYS`` after they were
    made when no video ever did. The row stays - its name, poster, script and
    prompt - so the person sees what expired and records or generates it
    again; a video already rendered keeps its own MP4.
    """

    #: Exempt from the retention: the person chose to keep it.
    keep: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    #: When a video last used it (a save naming it or dropping it, a render
    #: drawing it, the video deleted, the sweep finding a video using it).
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: When the retention deleted its media; ``None`` while they exist.
    expired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class _OwedOutcome:
    """The conversation a job's outcome is owed to.

    Set by ``jobs.enqueue`` when a chat turn starts the job (or starts it
    again), ``None`` for a job started anywhere else. Cleared once that
    conversation was told how the job ended - a tool's answer
    (``jobs.mark_told``), the wake that resumes it
    (``services/video_studio/wake.py``) - or when the job is cancelled. No
    foreign key and no index: a deleted conversation leaves a value no wake
    finds, and the wake reads it only for the jobs of one conversation.
    """

    owed_conversation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)


class VideoTake(_RetainedMedia, _OwedOutcome, Base):
    """One scripted recording of the backend browser."""

    __tablename__ = "video_takes"
    __table_args__ = (Index("ix_video_takes_user_created", "user_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    profile_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("browser_profiles.id", ondelete="SET NULL"), nullable=True, index=True
    )
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=JOB_PENDING, server_default=JOB_PENDING)
    #: The validated recording script (``spec.TakeScript``).
    script: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    #: The steps and recording parameters exactly as ``record_browser_demo`` was given them
    #: (``take_script.submitted``: ``{steps, params}``), read back by ``get_video_recording`` and copied by a re-record;
    #: none for a take recorded before it was kept.
    submitted_script: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), nullable=True, default=None)
    #: The action log the production reads (``capture.TakeEvent`` dumps).
    events: Mapped[list | None] = mapped_column(JSON(none_as_null=True), nullable=True, default=None)
    #: The cursor path, ``[[t_ms, x, y], ...]`` in CSS pixels of the viewport.
    cursor: Mapped[list | None] = mapped_column(JSON(none_as_null=True), nullable=True, default=None)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    viewport_width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    viewport_height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pixel_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: How far down the recorded page keeps its layout (``capture.probe_layout``): ``{width, holds_down_to,
    #: changes_at, why}``; none for a take recorded before the probe, or one it could not read.
    layout: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), nullable=True, default=None)
    fps: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: Frames the browser painted (the capture's real rate is this / duration).
    captured_frames: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: How fast it captured what its page painted (``capture_rate.measure``): ``{fps, rates, low, sparse,
    #: backend, pixel_ratio, preflight}``, with what it opened on (``opened``: its page state reset), the
    #: ``fps_fallback`` it took (``fallback``) and its wait in the recording lane (``waited``); none for a take
    #: recorded before. A take a step failed keeps ``opened``.
    capture: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), nullable=True, default=None)
    file_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    poster_file_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    contact_sheet_file_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=sa.text("0"))
    stage: Mapped[str | None] = mapped_column(String(200), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    execution_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: The worker's last progress write: a job it has written one for
    #: is never reported as queued.
    progress_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class VideoClip(_RetainedMedia, _OwedOutcome, Base):
    """One AI-generated shot (a video model's clip), a scene of a video like a recording."""

    __tablename__ = "video_clips"
    __table_args__ = (Index("ix_video_clips_user_created", "user_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    #: What the person calls it; ``None`` reads as its prompt.
    name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    model: Mapped[str] = mapped_column(String(120), nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    #: Aspect ratio, duration, resolution, negative prompt, reference image.
    params: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    #: The media it is made from, ``"<kind>:<id>"`` (``clips.source_refs``): its start frame and what it
    #: was cut from, its reference images, its ingredients. Written on every flush of the row
    #: (:func:`_index_source_refs`); a video playing it needs them too (``retention.media_index``). NULL only
    #: for a row written before it, read from its params meanwhile.
    source_refs: Mapped[list | None] = mapped_column(JSON(none_as_null=True), nullable=True, default=None)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=JOB_PENDING, server_default=JOB_PENDING)
    provider_job_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    has_audio: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    file_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    poster_file_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=sa.text("0"))
    stage: Mapped[str | None] = mapped_column(String(200), nullable=True)
    #: What the provider last said of the upstream job: ``queued`` /
    #: ``in_progress`` / ``running``, ``rate_limited`` / ``unavailable`` while
    #: its trouble is ridden out. ``None`` before the job exists upstream.
    provider_state: Mapped[str | None] = mapped_column(String(40), nullable=True)
    #: When the shot should be made, extrapolated from the progress the
    #: provider reports; ``None`` when it reports none.
    provider_eta_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    execution_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    #: The worker's last progress write, as on a take.
    progress_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


@sa.event.listens_for(VideoClip, "before_insert")
@sa.event.listens_for(VideoClip, "before_update")
def _index_source_refs(_mapper: object, _connection: object, target: VideoClip) -> None:
    """Every write of a shot stores what it is made from (``clips.source_refs``); a change is said to the videos."""
    from app.services.video_studio.clips import source_refs

    refs = source_refs(dict(target.params or {}))
    state = sa.inspect(target)
    if refs != state.attrs.source_refs.loaded_value:
        target.source_refs = refs
        # The videos that play it need them: one written in the same flush may have been indexed before it.
        target.__dict__["_sources_moved"] = bool(refs) or state.persistent


@sa.event.listens_for(VideoClip, "after_insert")
@sa.event.listens_for(VideoClip, "after_update")
def _reindex_users(_mapper: object, connection: object, target: VideoClip) -> None:
    """The videos playing a shot whose sources moved need its new ones (``retention.reindex_users_of``)."""
    if not target.__dict__.pop("_sources_moved", False):
        return
    from app.services.video_studio.retention import reindex_users_of
    from app.services.video_studio.spec import asset_ref

    key = asset_ref("clip", target.id)
    if key:
        reindex_users_of(connection, key)


class VideoMusic(_OwedOutcome, Base):
    """One AI-composed music track: a bed any video of its owner plays under its scenes.

    A few megabytes, reused across videos for months: outside the media
    retention, which is for the heavy recordings and shots.
    """

    __tablename__ = "video_music"
    __table_args__ = (Index("ix_video_music_user_created", "user_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    #: The owner, without a foreign key: the migration then takes no lock on ``users``.
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    #: What the person calls it; ``None`` reads as its prompt.
    name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    model: Mapped[str] = mapped_column(String(120), nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    #: The provider id, the length asked, instrumental, the negative prompt;
    #: once made, what the model said of the piece and the provider's notes.
    params: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=JOB_PENDING, server_default=JOB_PENDING)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    file_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=sa.text("0"))
    stage: Mapped[str | None] = mapped_column(String(200), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    execution_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    #: The worker's last progress write, as on a take.
    progress_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class VideoProject(VisibilityColumns, Base):
    """The edit: a validated timeline that references recordings, clips, slides and images."""

    __tablename__ = "video_projects"
    __table_args__ = (
        Index("ix_video_projects_is_company_managed", "is_company_managed"),
        Index("ix_video_projects_asset_refs_jsonb_gin", sa.text("(asset_refs::jsonb)"), postgresql_using="gin"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    #: The validated ``spec.VideoSpec`` (JSON-mode dump).
    spec: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    #: Every medium the video needs, ``"<kind>:<id>"``
    #: (``retention.media_index``): the recordings, AI shots, AI tracks and files
    #: the spec names, what those shots are made from, and what its ready renders
    #: drew. GIN-indexed: which videos use a medium is one indexed lookup, never
    #: a scan of the specs. Written on every flush of the row
    #: (:func:`_index_asset_refs`); NULL only for a row written by an older
    #: replica, which the media retention indexes before it reads.
    asset_refs: Mapped[list | None] = mapped_column(JSON(none_as_null=True), nullable=True, default=None)
    #: The rule ``asset_refs`` was written by (``spec.ASSET_REFS_VERSION``): an older one is indexed again by the
    #: media retention (``retention.index_stale_refs``), the index it had kept meanwhile.
    asset_refs_version: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default=sa.text("1"))
    scene_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=sa.text("0"))
    #: The planned duration of the current revision, before rendering.
    estimated_duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: The current render: the one the cards, the chat player and the public
    #: link play. Each render that finishes becomes it; a manager may pick an
    #: older one. The render retention never deletes it.
    latest_render_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    #: The video template the project wears (``video_studio.templates``): the
    #: spec's ``template_id``, else the platform default when it was created;
    #: ``None`` for a free-style video. SET NULL: deleting a template leaves its
    #: videos free-style, never deletes them.
    template_container_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("template_containers.id", ondelete="SET NULL"), nullable=True, index=True
    )
    #: The scenes a scene preview or a whole render drew (``render_checks.record_seen``):
    #: ``{scene id: {digest, render_id, at}}``, the digest of what each was then (``render_checks.scene_digests``).
    #: On the video, not on the preview rows the render retention deletes; NULL until a scene is drawn.
    scene_previews: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), nullable=True, default=None)
    updated_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


@sa.event.listens_for(VideoProject, "before_insert")
@sa.event.listens_for(VideoProject, "before_update")
def _index_asset_refs(_mapper: object, connection: object, target: VideoProject) -> None:
    """Every write of a project indexes the media it needs, whichever path wrote it (``retention.media_index``)."""
    from app.services.video_studio.retention import media_index
    from app.services.video_studio.spec import ASSET_REFS_VERSION

    target.asset_refs = media_index(connection, dict(target.spec or {}), target.id)
    target.asset_refs_version = ASSET_REFS_VERSION


class VideoRender(_OwedOutcome, Base):
    """One production of a project at one revision and one quality preset."""

    __tablename__ = "video_renders"
    __table_args__ = (Index("ix_video_renders_project_created", "project_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("video_projects.id", ondelete="CASCADE"), nullable=False
    )
    #: Who asked for it: the render reads the project's assets as this user.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    quality: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=JOB_PENDING, server_default=JOB_PENDING)
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=sa.text("0"))
    stage: Mapped[str | None] = mapped_column(String(200), nullable=True)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fps: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    file_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    poster_file_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    captions_file_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    #: Whether the captions are already in the frame: a player then leaves the
    #: WebVTT track off, never showing them twice. NULL (older renders)
    #: reads as burned in, the default those renders had.
    captions_burned_in: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=None)
    #: What the render could not do as asked (no speech provider, a missing
    #: slide), shown with the result rather than failing the render.
    notes: Mapped[list | None] = mapped_column(JSON(none_as_null=True), nullable=True, default=None)
    #: Exempt from the render retention: a manager chose to keep it.
    keep: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: The render of the same video that cancelled this one when it started:
    #: a video has one render in progress at most. No foreign key:
    #: the retention deletes either one on its own.
    superseded_by_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    #: A draft preview of one scene: the id of the scene in the effective spec. NULL for a whole
    #: render; a scene preview is never what the video plays and has a retention of its own.
    scene_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: A scene preview's first, middle and last frames (files of the requester), in that order.
    frame_file_ids: Mapped[list | None] = mapped_column(JSON(none_as_null=True), nullable=True, default=None)
    #: A whole render's chapters, one per scene: where it takes over and its own words
    #: (``timeline.chapters``). NULL for a scene preview and for the renders made before it.
    chapters: Mapped[list | None] = mapped_column(JSON(none_as_null=True), nullable=True, default=None)
    #: The music it played (``music.played_record``): its source and, for an AI track, the file -
    #: a refit composes the same track into another one. NULL for the renders made before it.
    music: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), nullable=True, default=None)
    #: The media the spec it drew named (``spec.asset_refs``): while it is ready they stay in its video's
    #: index (``retention.media_index``), whatever revision the video moved on to. NULL before it ended ready.
    asset_refs: Mapped[list | None] = mapped_column(JSON(none_as_null=True), nullable=True, default=None)
    #: What the render's audio and picture checks found (``render_checks.Checks``): the narrations heard
    #: back, the voice-over transcribed, stutters and frozen pictures, each finding with its time in the MP4.
    checks: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), nullable=True, default=None)
    #: A render started past its quality gate (``force``): who forced it, when and what the gate said.
    gate_override: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), nullable=True, default=None)
    #: The times it stood still and was resumed in place, its finished frames kept
    #: (``render_process.record_stall``): where it stood, for how long, the frames kept. NULL when it never stalled.
    stalls: Mapped[list | None] = mapped_column(JSON(none_as_null=True), nullable=True, default=None)
    #: Each time another worker took it over (``render_process.record_relaunch``): the attempt, what it resumed
    #: after, where it stood and the frame its drawing resumed from once known. NULL when no worker change happened.
    relaunches: Mapped[list | None] = mapped_column(JSON(none_as_null=True), nullable=True, default=None)
    #: What the pre-flight said before it was queued (``render_preflight.Preflight.as_dict``): how long, on how
    #: many pages, said by ``get_video_jobs``. NULL for the renders before it. A scene preview has none, except
    #: ``infra``: what its worker needs so its previews start at once, when it waited for memory.
    preflight: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), nullable=True, default=None)
    #: What its drawing measured (``render_preflight.measurement``): the pages and CPUs it drew on,
    #: the frames it drew in how long, the other stages' seconds - what the next pre-flights estimate from -
    #: beside what it reused: the frames kept from an earlier attempt's segments, the figure frames
    #: drawn and served, and each figure's reuse index entry (``reuse_index``).
    measured: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), nullable=True, default=None)
    #: Why the platform, not the video, failed it (``render_retry.failure_record``): its cause - Chromium, the
    #: lease, memory, a crash, a stall - and whether it ran again once first. NULL for a render its video failed.
    failure: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), nullable=True, default=None)
    #: What a whole render draws (``projects.carry_renders``): the revision its attempt read and each scene's
    #: drawn digest then (``render_checks.SceneDigests.drawn``) with its music's sources; once a later revision that
    #: changes nothing drawn carried it, ``carried_from``, that revision's effective spec it mixes its sound from and
    #: the scene digests it records. NULL for a scene preview and the renders before it.
    drawing: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), nullable=True, default=None)
    execution_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: When the render last moved forward: its supervisor resumes it in place once this is too old,
    #: and fails it as stalled past its resumes.
    progress_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class VideoRenderRange(Base):
    """One scene range of a render drawn on several workers (``services/video_studio/render_ranges.py``).

    A render one worker cannot draw within the 4 h a render may run is cut
    into scene ranges, each drawn and encoded on a worker of its own, its
    segments kept beside the render in the object store, then joined by the
    render. Whoever moves a range from ``pending`` to ``running`` draws it: a
    range execution on another worker, or the render itself when no worker took
    it. No foreign key (``video_renders`` is write-hot): a render that ends
    deletes its ranges, with the hourly sweep as a backstop.
    """

    __tablename__ = "video_render_ranges"
    __table_args__ = (
        sa.UniqueConstraint("render_id", "digest", "part", name="uq_video_render_ranges_part"),
        Index("ix_video_render_ranges_render", "render_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    render_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    #: What the ranges are drawn from (``render_ranges.Bundle.digest``): a render planned anew has ranges of its own.
    digest: Mapped[str] = mapped_column(String(64), nullable=False)
    #: Its place among the render's ranges, from 0.
    part: Mapped[int] = mapped_column(Integer, nullable=False)
    #: The frames ``[first_frame, end_frame)`` it draws.
    first_frame: Mapped[int] = mapped_column(Integer, nullable=False)
    end_frame: Mapped[int] = mapped_column(Integer, nullable=False)
    #: The ids of the scenes it shows, in timeline order.
    scenes: Mapped[list | None] = mapped_column(JSON(none_as_null=True), nullable=True, default=None)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=JOB_PENDING, server_default=JOB_PENDING)
    frames_done: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=sa.text("0"))
    #: Who draws it: ``render`` (the render's own process), else the worker holding its execution.
    drawn_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    execution_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: What its drawing measured (``render_preflight.measurement``).
    measured: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), nullable=True, default=None)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    progress_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class VideoProjectRevision(Base):
    """The spec of one video as one save left it: written once, never updated.

    Every save that makes a revision (``projects.save_spec``) adds a row, so
    ``get_video(revision=N)`` reads the spec at N and ``edit_video``'s
    ``restore_scene`` copies a scene back from it. ``changed`` maps a scene id
    (``video`` for the top-level keys) to the field paths the save changed.
    The newest :data:`app.services.video_studio.revisions.KEPT_REVISIONS` per
    video are kept. Written only by ``services/video_studio/revisions.py``.
    """

    __tablename__ = "video_project_revisions"
    __table_args__ = (sa.UniqueConstraint("project_id", "revision", name="uq_video_project_revisions_revision"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("video_projects.id", ondelete="CASCADE"), nullable=False
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    #: The authored spec as stored at that revision (``VideoProject.spec``).
    spec: Mapped[dict] = mapped_column(JSON, nullable=False)
    #: One line per change, as ``edit_video`` says them; derived from :attr:`changed` for a whole-spec save.
    summary: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    #: ``scene id (or video) -> [field path]`` changed by the save.
    changed: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    #: Who saved it (an e-mail), kept without a foreign key, and through what: ``agent`` or ``studio``.
    author_label: Mapped[str | None] = mapped_column(String(320), nullable=True)
    via: Mapped[str] = mapped_column(String(16), nullable=False, default="studio")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)


class VideoPublicLink(Base):
    """The public link of one video: anyone holding the URL may watch its current render.

    The deck idiom (``PresentationPublicLink``): at most one per project, the
    token looked up by its hash and kept Fernet-encrypted so a manager can copy
    the link again. Regenerating rewrites the token in place, turning the link
    off deletes the row. Written only by ``services/video_studio/public_links.py``.
    """

    __tablename__ = "video_public_links"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("video_projects.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    #: ``security.hash_token`` of the URL token; the raw token never touches the database.
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    #: ``security.encrypt_value`` of the same token (invariant 2).
    token_enc: Mapped[str] = mapped_column(Text, nullable=False)
    #: Whether a visitor may also download the MP4 and its captions. Off on a
    #: new link, kept by a regenerate.
    allow_download: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)


class VideoPronunciationLexicon(Base):
    """One version of the platform's pronunciation lexicon: written once, never updated.

    The newest version is the lexicon every video's voice-over reads, under
    its own ``voiceover.pronunciations``. An administrator's save adds a
    version, a restore adds one holding an older version's entries; the first
    is seeded from ``services/video_studio/data/pronunciation_lexicon.json``.
    Written only by ``services/video_studio/lexicon_store.py``. The author is
    kept without a foreign key: a deleted account leaves its versions as they
    are, named by the label they were saved with.
    """

    __tablename__ = "video_pronunciation_lexicons"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    version: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    #: ``[{text, say, ipa?, match, apply_to}]`` (``spec.Pronunciation``).
    entries: Mapped[list] = mapped_column(JSON, nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_by_label: Mapped[str | None] = mapped_column(String(320), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
