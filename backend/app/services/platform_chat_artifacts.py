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

"""The decks, reports and videos an answer shows, for the embedded chat.

The platform chat API (``api/v1/platform.py``) answers the chat panel of
OpenCTI, OpenAEV and OpenCRQ, through each product's proxy. An answer that made
a deck, a report or a video carries the web chat's markers
(``[[PRESENTATION:<id>]]``, ``[[REPORT:<id>]]``, ``[[VIDEO:<id>]]``, the
grammar of ``channels.base``):

* a panel that promises to render them (``supports_artifacts``) keeps them in
  the text and receives one descriptor per artifact (:func:`describe_answers`);
* an older panel receives them as links to the XTM One viewer, the links Slack
  and Teams get (:func:`markers_as_links`), and never sees one streamed
  (:class:`ArtifactMarkerStreamFilter`).

A marker is LLM text, so an answer describes only the rows of its own
conversation or of its user (``channels.base.artifact_scope_filter``). An
artifact read by id (:func:`describe_artifact`, :func:`download_response`)
follows that artifact's own reading rule instead: the deck's three tiers, the
report's owner, the video's three tiers.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.base import (
    _DASHBOARD_MARKER_RE,
    _PRESENTATION_MARKER_RE,
    _REPORT_MARKER_RE,
    _VIDEO_MARKER_RE,
    artifact_scope_filter,
    resolve_channel_dashboard_markers,
    resolve_channel_presentation_markers,
    resolve_channel_report_markers,
    resolve_channel_video_markers,
)
from app.core.utils import safe_uuid

PRESENTATION = "presentation"
REPORT = "report"
VIDEO = "video"
ARTIFACT_KINDS: tuple[str, ...] = (PRESENTATION, REPORT, VIDEO)

_MARKERS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (PRESENTATION, _PRESENTATION_MARKER_RE),
    (REPORT, _REPORT_MARKER_RE),
    (VIDEO, _VIDEO_MARKER_RE),
)

#: Markers no panel renders, whatever it promises: a custom dashboard is a web
#: page of widgets, so every panel gets a link to it (:func:`link_only_markers_as_links`)
#: and never sees the marker streamed.
_LINK_ONLY_MARKERS: tuple[re.Pattern[str], ...] = (_DASHBOARD_MARKER_RE,)

#: The web-app page of each kind, the one the channel links open.
_VIEWER_PATHS: dict[str, str] = {PRESENTATION: "presentations", REPORT: "reports", VIDEO: "videos"}

#: What a report and a video download as; a deck takes every export format.
REPORT_DOWNLOAD_FORMATS: tuple[str, ...] = ("pdf",)
VIDEO_DOWNLOAD_FORMATS: tuple[str, ...] = ("mp4",)


def download_formats(kind: str) -> tuple[str, ...]:
    """The formats an artifact of *kind* may be downloaded in (a video: once rendered)."""
    if kind == PRESENTATION:
        from app.services.presentation_export import EXPORT_FORMATS

        return EXPORT_FORMATS
    return {REPORT: REPORT_DOWNLOAD_FORMATS, VIDEO: VIDEO_DOWNLOAD_FORMATS}.get(kind, ())


@dataclass(frozen=True, slots=True)
class MarkerRef:
    """One artifact an answer shows: its kind, its id and the slide a deck marker names."""

    kind: str
    id: uuid.UUID
    slide_ref: str | None = None


def marker_refs(text: str | None) -> list[MarkerRef]:
    """The artifacts *text* shows, in the order their markers stand, each once.

    A deck named twice keeps its first place and the first slide a marker names.
    """
    if not text or "[[" not in text:
        return []
    found: list[tuple[int, MarkerRef]] = []
    for kind, pattern in _MARKERS:
        for match in pattern.finditer(text):
            slide = match.group(2) if pattern.groups >= 2 else None
            found.append((match.start(), MarkerRef(kind, uuid.UUID(match.group(1).lower()), slide or None)))
    found.sort(key=lambda item: item[0])
    ordered: dict[tuple[str, uuid.UUID], MarkerRef] = {}
    for _, ref in found:
        key = (ref.kind, ref.id)
        kept = ordered.get(key)
        if kept is None or (kept.slide_ref is None and ref.slide_ref):
            ordered[key] = ref
    return list(ordered.values())


def viewer_url(kind: str, artifact_id: object, slide_ref: str | None = None) -> str | None:
    """The XTM One page that shows the artifact, absolute; ``None`` without a frontend URL."""
    from app.core.config import settings

    base = (settings.frontend_url or "").rstrip("/")
    if not base:
        return None
    fragment = f"#/{slide_ref}" if slide_ref else ""
    return f"{base}/{_VIEWER_PATHS[kind]}/{artifact_id}{fragment}"


def _with_url(descriptor: dict, kind: str, artifact_id: object, slide_ref: str | None = None) -> dict:
    url = viewer_url(kind, artifact_id, slide_ref)
    if url:
        descriptor["url"] = url
    return descriptor


def presentation_descriptor(row: Any, slide_ref: str | None = None) -> dict:
    descriptor: dict = {
        "kind": PRESENTATION,
        "id": str(row.id),
        "title": row.title,
        "slide_count": int(row.slide_count or 0),
        "downloads": list(download_formats(PRESENTATION)),
    }
    if slide_ref:
        descriptor["slide_ref"] = slide_ref
    return _with_url(descriptor, PRESENTATION, row.id, slide_ref)


def report_descriptor(row: Any) -> dict:
    descriptor: dict = {
        "kind": REPORT,
        "id": str(row.id),
        "title": row.title,
        "section_count": int(row.block_count or 0),
        "downloads": list(REPORT_DOWNLOAD_FORMATS),
    }
    return _with_url(descriptor, REPORT, row.id)


def _playable(render: Any | None) -> bool:
    from app.models.video_studio import JOB_READY

    return render is not None and render.status == JOB_READY and render.file_id is not None


def video_descriptor(project: Any, *, latest: Any | None, current: Any | None) -> dict:
    """A video: its newest render's state, and the current render's length and file.

    *latest* is the newest render whatever its state (the one being made);
    *current* is the one the video plays (``latest_render_id``).
    """
    playable = _playable(current)
    duration = current.duration_ms if playable and current.duration_ms else project.estimated_duration_ms
    descriptor: dict = {
        "kind": VIDEO,
        "id": str(project.id),
        "title": project.title,
        "scene_count": int(project.scene_count or 0),
        "downloads": list(VIDEO_DOWNLOAD_FORMATS) if playable else [],
    }
    if duration:
        descriptor["duration_ms"] = int(duration)
    if latest is not None:
        descriptor["status"] = latest.status
        descriptor["progress"] = int(latest.progress or 0)
    elif playable:
        descriptor["status"] = current.status
    return _with_url(descriptor, VIDEO, project.id)


async def _video_renders(db: AsyncSession, projects: list[Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """The newest render and the current render of each project, keyed by project id."""
    from app.models.video_studio import VideoRender
    from app.services.video_studio.projects import latest_renders

    if not projects:
        return {}, {}
    latest = await latest_renders(db, [p.id for p in projects])
    current_ids = [p.latest_render_id for p in projects if p.latest_render_id]
    current_by_id: dict[uuid.UUID, Any] = {}
    if current_ids:
        rows = await db.execute(select(VideoRender).where(VideoRender.id.in_(current_ids)))
        current_by_id = {row.id: row for row in rows.scalars().all()}
    current = {str(p.id): current_by_id[p.latest_render_id] for p in projects if p.latest_render_id in current_by_id}
    return latest, current


async def describe_answers(
    db: AsyncSession,
    texts: list[str],
    *,
    conversation_id: object = None,
    user_id: object = None,
) -> list[list[dict]]:
    """One list of descriptors per text, in marker order: one read per kind for them all.

    A marker whose row is unknown or out of scope (another conversation's,
    another user's) is left undescribed: the panel renders nothing for it.
    """
    from app.models.presentation import Presentation
    from app.models.report import Report
    from app.models.video_studio import VideoProject

    refs_per_text = [marker_refs(text) for text in texts]
    wanted: dict[str, set[uuid.UUID]] = {}
    for refs in refs_per_text:
        for ref in refs:
            wanted.setdefault(ref.kind, set()).add(ref.id)
    if not wanted:
        return [[] for _ in texts]

    rows: dict[tuple[str, uuid.UUID], Any] = {}
    # The deck's spec is its one heavy column: a descriptor reads the counts only.
    columns = {
        PRESENTATION: (Presentation, (Presentation.id, Presentation.title, Presentation.slide_count)),
        REPORT: (Report, (Report.id, Report.title, Report.block_count)),
        VIDEO: (
            VideoProject,
            (
                VideoProject.id,
                VideoProject.title,
                VideoProject.scene_count,
                VideoProject.estimated_duration_ms,
                VideoProject.latest_render_id,
            ),
        ),
    }
    for kind, ids in wanted.items():
        model, selected = columns[kind]
        scope = artifact_scope_filter(model, conversation_id=conversation_id, user_id=user_id)
        if scope is None:
            continue
        result = await db.execute(select(*selected).where(model.id.in_(list(ids)), scope))
        for row in result.all():
            rows[(kind, row.id)] = row

    videos = [row for (kind, _), row in rows.items() if kind == VIDEO]
    latest, current = await _video_renders(db, videos)

    def _describe(ref: MarkerRef) -> dict | None:
        row = rows.get((ref.kind, ref.id))
        if row is None:
            return None
        if ref.kind == PRESENTATION:
            return presentation_descriptor(row, ref.slide_ref)
        if ref.kind == REPORT:
            return report_descriptor(row)
        return video_descriptor(row, latest=latest.get(str(row.id)), current=current.get(str(row.id)))

    return [[d for ref in refs if (d := _describe(ref)) is not None] for refs in refs_per_text]


async def markers_as_links(
    db: AsyncSession,
    text: str,
    *,
    conversation_id: object = None,
    user_id: object = None,
    language: str | None = None,
) -> str:
    """*text* with each deck, report, video and dashboard marker as a link to its XTM One page.

    For a panel that cannot render the markers: the links Slack and Teams get.
    """
    if not text or "[[" not in text:
        return text
    from app.core.config import settings

    scope = {"conversation_id": conversation_id, "user_id": user_id, "language": language}
    text = await resolve_channel_presentation_markers(db, text, settings.frontend_url, **scope)
    text = await resolve_channel_report_markers(db, text, settings.frontend_url, **scope)
    text = await resolve_channel_video_markers(db, text, settings.frontend_url, **scope)
    return await resolve_channel_dashboard_markers(db, text, settings.frontend_url, **scope)


async def link_only_markers_as_links(
    db: AsyncSession,
    text: str,
    *,
    conversation_id: object = None,
    user_id: object = None,
    language: str | None = None,
) -> str:
    """*text* with the markers no panel renders (a dashboard's) as links, the others kept.

    For a panel that renders decks, reports and videos: it gets those as
    descriptors and the dashboards as links.
    """
    if not text or "[[" not in text:
        return text
    from app.core.config import settings

    scope = {"conversation_id": conversation_id, "user_id": user_id, "language": language}
    return await resolve_channel_dashboard_markers(db, text, settings.frontend_url, **scope)


_MARKER_HEADS: tuple[str, ...] = ("[[PRESENTATION:", "[[REPORT:", "[[VIDEO:", "[[DASHBOARD:")
#: A marker cut short: the longest is a deck's, with a uuid and a slide id.
_PARTIAL_MARKER_RE = re.compile(
    r"\[\[(?:PRESENTATION|REPORT|VIDEO|DASHBOARD):[0-9A-F-]{0,36}(?:#(?:/[A-Za-z0-9_-]{0,64})?)?\]?",
    re.IGNORECASE,
)
_LINK_ONLY_HEADS: tuple[str, ...] = ("[[DASHBOARD:",)
_PARTIAL_LINK_ONLY_MARKER_RE = re.compile(r"\[\[DASHBOARD:[0-9A-F-]{0,36}\]?", re.IGNORECASE)
_MARKER_MAX_CHARS = len("[[PRESENTATION:") + 36 + len("#/") + 64 + len("]]")


class ArtifactMarkerStreamFilter:
    """Streamed text with the deck, report, video and dashboard markers left out.

    ``link_only=True`` (a panel that renders decks, reports and videos) leaves
    out only the markers no panel renders (a dashboard's): the ``done`` payload
    carries them as links.

    A marker can be split across chunks: a tail that may still become one is
    held until the next chunk decides, and :meth:`flush` releases what is held
    at the end of the segment (an unfinished marker is ordinary text).
    """

    def __init__(self, *, link_only: bool = False) -> None:
        self._held = ""
        if link_only:
            self._patterns = _LINK_ONLY_MARKERS
            self._heads = _LINK_ONLY_HEADS
            self._partial = _PARTIAL_LINK_ONLY_MARKER_RE
        else:
            self._patterns = (*(pattern for _, pattern in _MARKERS), *_LINK_ONLY_MARKERS)
            self._heads = _MARKER_HEADS
            self._partial = _PARTIAL_MARKER_RE

    def feed(self, chunk: str) -> str:
        text = self._held + (chunk or "")
        for pattern in self._patterns:
            text = pattern.sub("", text)
        self._held = ""
        for index in range(max(0, len(text) - _MARKER_MAX_CHARS), len(text)):
            if text[index] != "[":
                continue
            tail = text[index:]
            upper = tail.upper()
            if any(head.startswith(upper) for head in self._heads) or self._partial.fullmatch(tail):
                self._held = tail
                return text[:index]
        return text

    def flush(self) -> str:
        held, self._held = self._held, ""
        return held


async def load_readable_artifact(db: AsyncSession, kind: str, artifact_id: object, user: Any) -> Any | None:
    """The artifact *kind* / *artifact_id* if *user* may read it, else ``None``.

    The reading rule of each kind, as its agent tools apply it: a deck by the
    three tiers, a report by its owner, a video by the three tiers.
    """
    target = safe_uuid(str(artifact_id or "").strip())
    if target is None or kind not in ARTIFACT_KINDS:
        return None
    if kind == PRESENTATION:
        from app.services.presentations import load_visible_presentation

        found = await load_visible_presentation(db, target, user_id=user.id)
        return found[0] if found else None
    if kind == REPORT:
        from app.services.reports import load_owned_report

        return await load_owned_report(db, target, user_id=user.id)
    from app.services.video_studio.projects import load_visible_project

    return await load_visible_project(db, target, user)


async def describe_artifact(db: AsyncSession, kind: str, artifact_id: object, user: Any) -> dict | None:
    """The descriptor of one artifact *user* may read, as of now; ``None`` otherwise."""
    row = await load_readable_artifact(db, kind, artifact_id, user)
    if row is None:
        return None
    if kind == PRESENTATION:
        return presentation_descriptor(row)
    if kind == REPORT:
        return report_descriptor(row)
    latest, current = await _video_renders(db, [row])
    return video_descriptor(row, latest=latest.get(str(row.id)), current=current.get(str(row.id)))


class ArtifactDownloadError(Exception):
    """A download that cannot be answered, with the status and the reason the route returns."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


#: Said when the headless browser behind an export cannot be reached.
EXPORT_UNAVAILABLE = "The platform could not render the export. Open it in XTM One and try again later."


async def download_response(db: AsyncSession, kind: str, artifact_id: object, fmt: str, user: Any):
    """The file of an artifact *user* may read, as the route answers it.

    A deck or a report is rendered now (the caller's export budget, the
    connection released while the browser paints); a video downloads its
    current render. Raises :class:`ArtifactDownloadError`.
    """
    from fastapi.responses import Response

    from app.core.database import release_db_connection_before_loop
    from app.core.utils import build_content_disposition

    fmt = (fmt or "").strip().lower()
    if kind not in ARTIFACT_KINDS:
        raise ArtifactDownloadError(404, "Not found")
    if fmt not in download_formats(kind):
        raise ArtifactDownloadError(400, f"format must be one of: {', '.join(download_formats(kind))}")

    if kind == VIDEO:
        return await _video_download(db, artifact_id, user)

    from app.services.presentation_export import enforce_export_rate_limit

    await enforce_export_rate_limit(user.id)
    row = await load_readable_artifact(db, kind, artifact_id, user)
    if row is None:
        raise ArtifactDownloadError(404, "Not found")
    row_id, title, spec = row.id, row.title, (row.spec or {}) if kind == PRESENTATION else None
    await release_db_connection_before_loop(db)
    try:
        if kind == PRESENTATION:
            from app.services.presentation_export import export_presentation

            exported = await export_presentation(
                presentation_id=row_id, user_id=user.id, title=title, spec=spec, export_format=fmt
            )
        else:
            from app.services.report_export import export_report

            exported = await export_report(report_id=row_id, user_id=user.id, title=title)
    except Exception as exc:
        from app.services.presentation_export import PresentationRenderError
        from app.services.report_export import ReportRenderError

        if isinstance(exc, (PresentationRenderError, ReportRenderError)):
            raise ArtifactDownloadError(502, str(exc)) from exc
        raise ArtifactDownloadError(503, EXPORT_UNAVAILABLE) from exc
    return Response(
        content=exported.data,
        media_type=exported.content_type,
        headers={
            "Content-Disposition": build_content_disposition(exported.filename),
            "Cache-Control": "private, no-store",
        },
    )


async def _video_download(db: AsyncSession, artifact_id: object, user: Any):
    from app.core.utils import build_content_disposition
    from app.models.file_attachment import FileAttachment
    from app.models.video_studio import VideoRender
    from app.services.file_responses import file_response_headers
    from app.services.file_storage import FileNotFoundInStorageError
    from app.services.video_studio import media

    project = await load_readable_artifact(db, VIDEO, artifact_id, user)
    if project is None:
        raise ArtifactDownloadError(404, "Not found")
    render = await db.get(VideoRender, project.latest_render_id) if project.latest_render_id else None
    if not _playable(render):
        raise ArtifactDownloadError(409, "This video has no finished render yet.")
    row = await db.get(FileAttachment, render.file_id)
    if row is None:
        raise ArtifactDownloadError(404, "Not found")
    headers = file_response_headers(row, store=False)
    headers["Content-Disposition"] = build_content_disposition(
        media.download_filename(project.title, "video", height=render.height, fps=render.fps)
    )
    try:
        return await media.serve_file(row, None, headers)
    except FileNotFoundInStorageError:
        raise ArtifactDownloadError(404, "Not found") from None
