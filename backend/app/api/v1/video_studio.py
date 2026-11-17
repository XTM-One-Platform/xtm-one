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

"""Video Studio API: projects, renders, recordings, clips, music and media.

The writes of a project's spec go through ``projects.save_spec`` (the tools'
path), so a spec the renderer cannot draw is never stored. Recordings,
clips and AI tracks are their creator's; a project shares what it shows
(``projects.can_read_project_asset``). A recording signs in with a browser
profile the person may use (``api/v1/browser_profiles.py``). Media play
through confined tokens (``services/video_studio/media``) with byte ranges.
Rendering spends AI providers (the voice-over) and is behind the AI gate;
reading and recording are not.

Sharing: ``PATCH /projects/{id}`` writes the grants through the deck's
one sharing write (``presentation_sharing.apply_sharing``), and
``/projects/{id}/public-link`` is the Share dialog's side of
``services/video_studio/public_links`` - what the link then serves without an
account is ``api/v1/video_public.py``. Renders are kept under
``services/video_studio/render_retention``, whose days an administrator sets
(``GET`` / ``PUT /render-retention``); a manager keeps one, makes it the
current one or deletes it (``PATCH`` / ``DELETE /renders/{id}``), or cleans up
many at once (``POST /projects/{id}/renders/cleanup``, ``render_cleanup``).

The platform's pronunciation lexicon (``services/video_studio/lexicon_store``)
is read by every user (the editor lists it under a video's own entries) and
written by an administrator only, each save a new version, audited;
``POST /pronunciations/speak`` speaks one term as a video's voice-over would.
"""

from __future__ import annotations

import functools
import hashlib
import uuid
from datetime import datetime
from typing import Literal

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from sqlalchemy import Column, String, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_admin, require_ai_enabled
from app.api.v1.helpers import (
    can_manage,
    get_user_group_ids,
    parse_path_id,
    parse_uuid,
    parse_uuid_list,
    resolve_owner_names,
    user_visible_filter,
)
from app.api.v1.schemas.video_studio import (
    ClipCreate,
    ClipRetry,
    ClipStartFramePreview,
    ComposedPicture,
    JobList,
    LibraryPage,
    MediaAssetUpdate,
    MediaBulkDelete,
    MediaBulkDeleteResult,
    MediaTokenRequest,
    MediaTokenResponse,
    MusicCreate,
    MusicRetry,
    PictureCompose,
    PronunciationLexiconWrite,
    PronunciationSpeakRequest,
    RenderCleanupRequest,
    RenderRequest,
    RenderRetentionWrite,
    RenderUpdate,
    SceneRenderRequest,
    VideoOperationsWrite,
    VideoPreviewRequest,
    VideoProjectCreate,
    VideoProjectUpdate,
    VideoPublicLinkRequest,
    VideoPublicLinkResponse,
    VideoPublicLinkUpdate,
    VideoSpecWrite,
)
from app.core.base_path import with_base_path
from app.core.csp import PagePolicy, inline_script_hashes, register_page_policy
from app.core.database import async_session, get_db
from app.core.utils import build_content_disposition, fit_copy_name, safe_uuid
from app.models.file_attachment import FileAttachment
from app.models.user import User
from app.models.video_studio import (
    JOB_READY,
    JOB_TERMINAL_STATUSES,
    VideoClip,
    VideoMusic,
    VideoProject,
    VideoRender,
    VideoTake,
)
from app.services.ai_state import is_ai_enabled
from app.services.audit import log_audit
from app.services.video_studio import (
    assets,
    encoder,
    jobs,
    library,
    media,
    preview,
    projects,
    public_links,
    render_cleanup,
    render_retention,
    retention,
    templates,
)
from app.services.video_studio import music as music_tracks
from app.services.video_studio.spec import QUALITY_PRESETS, TITLE_MAX_LENGTH, VideoSpecError
from app.services.visibility import SegmentCounts, Visibility, resolve_visibility, segment_counts

router = APIRouter()

#: A copy's title is fitted to what a spec takes, not to the wider column.
_SPEC_TITLE = Column("title", String(TITLE_MAX_LENGTH))

_NOT_FOUND = "Not found"


def _not_found() -> HTTPException:
    return HTTPException(status_code=404, detail=_NOT_FOUND)


def _spec_error(exc: VideoSpecError) -> HTTPException:
    return HTTPException(
        status_code=422,
        detail={"message": str(exc), "problems": [{"path": p.path, "message": p.message} for p in exc.problems]},
    )


# ── Projects ──────────────────────────────────────────────────────────────


async def _visible_project(db: AsyncSession, project_id: str, user: User) -> VideoProject:
    project = await projects.load_visible_project(db, parse_path_id(project_id, not_found_detail=_NOT_FOUND), user)
    if project is None:
        raise _not_found()
    return project


async def _manageable_project(db: AsyncSession, project_id: str, user: User) -> VideoProject:
    project = await _visible_project(db, project_id, user)
    if not await projects.can_manage_project(db, project, user):
        raise HTTPException(status_code=403, detail="You can view this video but not change it.")
    return project


async def _project_payload(db: AsyncSession, project: VideoProject, user: User) -> dict:
    latest = (await projects.latest_renders(db, [project.id])).get(str(project.id))
    ready = await db.get(VideoRender, project.latest_render_id) if project.latest_render_id else None
    applied = await projects.effective_spec(db, project, user)
    manages = await projects.can_manage_project(db, project, user)
    payload = projects.serialize_project(
        project,
        can_manage=manages,
        latest=latest,
        ready=ready,
        template=applied.template,
        applied=applied,
        permission=projects.video_permission(project, user, can_manage=manages),
        owner_name=(await resolve_owner_names(db, [project])).get(str(project.user_id)),
        has_public_link=str(project.id) in await public_links.public_video_ids(db, [project.id]),
    )
    payload["render_retention"] = (await render_retention.load_policy(db)).as_dict()
    # What the studio keys a revision's quality report by: moved, a report kept from before is read again.
    from app.services.video_studio.quality import QUALITY_REPORT_VERSION

    payload["quality_version"] = QUALITY_REPORT_VERSION
    return payload


async def _template_names(db: AsyncSession, rows: list[VideoProject], user: User) -> dict[str, object]:
    """The templates the projects wear, as *user* may see them, by id."""
    from app.models.template_container import TemplateContainer

    ids = {p.template_container_id for p in rows if p.template_container_id}
    if not ids:
        return {}
    groups = await get_user_group_ids(db, user.id)
    found = await db.execute(
        select(TemplateContainer).where(
            TemplateContainer.id.in_(ids),
            user_visible_filter(TemplateContainer, user.id, groups, is_admin=user.role == "admin"),
        )
    )
    return {str(row.id): row for row in found.scalars().all()}


# The orders the library's data table offers (``GET /video-studio/projects?sort=``).
_PROJECT_SORT_COLUMNS = {
    "updated_at": VideoProject.updated_at,
    "created_at": VideoProject.created_at,
    "title": func.lower(VideoProject.title),
    "scene_count": VideoProject.scene_count,
}

# The render states the library's data table filters on (``GET /video-studio/projects?render=``):
# a video is rendered once it has a ready render (its current one, ``latest_render_id``).
_PROJECT_RENDER_FILTERS = {
    "rendered": VideoProject.latest_render_id.is_not(None),
    "not_rendered": VideoProject.latest_render_id.is_(None),
}


def _render_filter(render: str | None):
    """The ``render`` clause of the library and of its segment counts; an unknown value is a 422."""
    if render is None:
        return None
    clause = _PROJECT_RENDER_FILTERS.get(render)
    if clause is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"render must be one of {sorted(_PROJECT_RENDER_FILTERS)}",
        )
    return clause


@router.get("/projects")
async def list_projects(
    search: str | None = Query(default=None, max_length=200, description="Words of the title, template or owner."),
    vis: str | None = Query(
        default=None,
        description="Visibility segment: `mine`, `shared`, `company` or `all` (default `all`). See services/visibility.",
    ),
    visibility: library.VideoVisibility | None = Query(
        default=None,
        deprecated=True,
        description="[DEPRECATED] alias of `vis`, read when `vis` is absent.",
    ),
    limit: int = Query(default=library.PROJECT_PAGE_SIZE, ge=1, le=library.MAX_PAGE_SIZE),
    offset: int = Query(default=0, ge=0),
    sort: str = Query(
        default="updated_at",
        description="Order by `updated_at` (default), `created_at`, `title` or `scene_count`.",
    ),
    direction: str = Query(
        default="desc", description="`asc` or `desc` (default); the last changed video breaks ties."
    ),
    render: str | None = Query(
        default=None,
        description="`rendered`: only the videos with a ready render; `not_rendered`: only those without one.",
    ),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> LibraryPage:
    """The videos the caller can open, a page at a time, the last changed first.

    ``sort`` and ``direction`` order the whole match before the page is cut
    (the library's data table sorts on the server); ``render`` keeps the videos
    with (or without) a ready render; an unknown value is a 422.
    """
    render_clause = _render_filter(render)
    order_column = _PROJECT_SORT_COLUMNS.get(sort)
    if order_column is None or direction not in ("asc", "desc"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"sort must be one of {sorted(_PROJECT_SORT_COLUMNS)} and direction asc or desc",
        )
    order = order_column.asc() if direction == "asc" else order_column.desc()
    # The last changed video breaks ties; a sort on ``updated_at`` already orders by it.
    order_by = [order] if sort == "updated_at" else [order, VideoProject.updated_at.desc()]
    order_by.append(VideoProject.id.desc())
    groups = await get_user_group_ids(db, user.id)
    where = library.project_filters(user, groups, search=search, visibility=resolve_visibility(vis, visibility))
    if render_clause is not None:
        where.append(render_clause)
    total = (await db.execute(select(func.count()).select_from(VideoProject).where(*where))).scalar_one()
    rows = (
        (await db.execute(select(VideoProject).where(*where).order_by(*order_by).offset(offset).limit(limit)))
        .scalars()
        .all()
    )
    latest = await projects.latest_renders(db, [p.id for p in rows])
    ready_ids = [p.latest_render_id for p in rows if p.latest_render_id]
    ready: dict[str, VideoRender] = {}
    if ready_ids:
        for render in (await db.execute(select(VideoRender).where(VideoRender.id.in_(ready_ids)))).scalars().all():
            ready[str(render.id)] = render
    worn = await _template_names(db, list(rows), user)
    owners = await resolve_owner_names(db, list(rows))
    public = await public_links.public_video_ids(db, [p.id for p in rows])
    items = []
    for p in rows:
        manages = can_manage(user, p, groups)
        items.append(
            projects.serialize_project(
                p,
                can_manage=manages,
                latest=latest.get(str(p.id)),
                ready=ready.get(str(p.latest_render_id)) if p.latest_render_id else None,
                template=worn.get(str(p.template_container_id)) if p.template_container_id else None,
                permission=projects.video_permission(p, user, can_manage=manages),
                owner_name=owners.get(str(p.user_id)) if p.user_id else None,
                has_public_link=str(p.id) in public,
            )
        )
    return LibraryPage(items=items, total=total)


@router.get("/projects/segment-counts", response_model=SegmentCounts)
async def project_segment_counts(
    search: str | None = Query(default=None, max_length=200),
    render: str | None = Query(default=None, description="Same render filter as `GET /projects`."),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SegmentCounts:
    """How many videos each visibility segment holds under the list's search and render filter.

    Declared before ``GET /projects/{project_id}`` so the literal wins the match.
    """
    render_clause = _render_filter(render)
    groups = await get_user_group_ids(db, user.id)

    async def count(segment: Visibility) -> int:
        where = library.project_filters(user, groups, search=search, visibility=segment)
        if render_clause is not None:
            where.append(render_clause)
        return (await db.execute(select(func.count()).select_from(VideoProject).where(*where))).scalar_one()

    return await segment_counts(count)


async def _create_from_spec(
    db: AsyncSession, user: User, spec: dict, *, summary: str, held: dict | None = None
) -> projects.SavedSpec:
    try:
        saved = await projects.save_spec(db, user=user, raw=spec, held=held)
    except VideoSpecError as exc:
        raise _spec_error(exc) from None
    project = saved.project
    await log_audit(
        db,
        user=user,
        action="create",
        entity_type="video_project",
        entity_id=str(project.id),
        entity_name=project.title,
        summary=summary.format(title=project.title),
    )
    return saved


@router.post("/projects", status_code=status.HTTP_201_CREATED)
async def create_project(
    body: VideoProjectCreate, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> dict:
    """A video made by hand in the editor (blank, from a template, or from media): the tools' validator."""
    saved = await _create_from_spec(db, user, body.spec, summary="Created video {title}")
    await db.commit()
    await projects.queue_figure_stills(saved, user)
    return await _project_payload(db, saved.project, user)


@router.post("/projects/{project_id}/duplicate", status_code=status.HTTP_201_CREATED)
async def duplicate_project(
    project_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> dict:
    """A copy of a video the caller can open, theirs and private; its media stay shared by the two."""
    source = await _visible_project(db, project_id, user)
    spec = dict(source.spec or {})
    spec["title"] = fit_copy_name(source.title, _SPEC_TITLE, suffix=" (copy)")
    saved = await _create_from_spec(db, user, spec, summary="Duplicated a video as {title}", held=dict(source.spec or {}))
    await db.commit()
    await projects.queue_figure_stills(saved, user)
    return await _project_payload(db, saved.project, user)


@router.post("/projects/{project_id}/preview")
async def preview_project(
    project_id: str,
    body: VideoPreviewRequest | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """The editor's live preview: the render's timeline document of the draft (or the stored video).

    A draft is previewed by who may change the video; the media play from
    confined URLs (``preview.build_preview``). Nothing is stored, except the
    narrations a draft's preview speaks into the voice cache the render reads
    and the figure stills it draws.
    """
    project = await _visible_project(db, project_id, user)
    draft = body.spec if body is not None else None
    if draft is not None and not await projects.can_manage_project(db, project, user):
        raise HTTPException(status_code=403, detail="You can view this video but not change it.")
    # The preview itself answers with AI off; only the speaking of a draft's narrations needs it.
    speak = draft is not None and await is_ai_enabled(db)
    try:
        return await preview.build_preview(db, project, user, draft, speak=speak, draw=draft is not None)
    except VideoSpecError as exc:
        raise _spec_error(exc) from None


_PREVIEW_PAGE_PATH = "/api/v1/video-studio/preview-page"


@functools.cache
def _preview_page() -> str:
    """The compositor page the editor frames, built once: constants and the bundled fonts, no data."""
    from app.services.builtin_tools.html_render import font_face_css
    from app.services.video_studio.compositor_page import page_html

    return page_html(font_face_css())


@functools.cache
def _preview_page_hashes() -> tuple[str, ...]:
    return inline_script_hashes(_preview_page())


register_page_policy(_PREVIEW_PAGE_PATH, PagePolicy(script_hashes=_preview_page_hashes, frame_ancestors="self"))


@functools.cache
def _preview_page_etag() -> str:
    return '"' + hashlib.sha256(_preview_page().encode("utf-8")).hexdigest()[:32] + '"'


@router.get("/preview-page", include_in_schema=False)
async def preview_page(request: Request) -> Response:
    """The render's compositor, for the editor's preview frame.

    Public like a static asset: the page carries no data - the editor, signed
    in, hands it the document and the confined media URLs - and only the
    platform may frame it (its page policy). Revalidated on every load
    against the ETag of its content: a page kept for an hour drew a
    new template with the compositor of the previous release (a closing
    card's ``*accent*`` shown as asterisks); an unchanged page costs a 304.
    """
    etag = _preview_page_etag()
    headers = {"Cache-Control": "no-cache", "ETag": etag, "X-Content-Type-Options": "nosniff"}
    if etag in (request.headers.get("if-none-match") or ""):
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)
    return Response(content=_preview_page(), media_type="text/html; charset=utf-8", headers=headers)


@router.get("/templates")
async def list_templates(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> JobList:
    """The video templates the caller may pick, the platform default first."""
    rows = await templates.list_video_templates(db, user)
    default = await templates.default_template(db)
    default_id = default.id if default is not None else None
    return JobList(items=[templates.template_summary(row, is_default=row.id == default_id) for row in rows])


@router.get("/projects/{project_id}")
async def get_project(
    project_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> dict:
    return await _project_payload(db, await _visible_project(db, project_id, user), user)


@router.get("/projects/{project_id}/quality")
async def get_project_quality(
    project_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> dict:
    """The quality check of the saved video: what covers what, what does not fit or is read too fast.

    The same check the agent reads after a save, for the editor; a report of
    an unchanged revision is served from its short cache.
    """
    from app.services.video_studio.quality import check_video_quality

    project = await _visible_project(db, project_id, user)
    report = await check_video_quality(project.id, user.id, expected_revision=project.revision)
    return report.as_dict()


@router.put("/projects/{project_id}/spec")
async def put_project_spec(
    project_id: str, body: VideoSpecWrite, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> dict:
    project = await _manageable_project(db, project_id, user)
    try:
        saved = await projects.save_spec(db, user=user, raw=body.spec, project=project)
    except VideoSpecError as exc:
        raise _spec_error(exc) from None
    if not saved.unchanged:
        await log_audit(
            db,
            user=user,
            action="update",
            entity_type="video_project",
            entity_id=str(project.id),
            entity_name=project.title,
            summary=f"Updated video {project.title}",
            details={"revision": project.revision},
        )
    await db.commit()
    if saved.animations:
        await jobs.wake()
    await projects.queue_figure_stills(saved, user)
    return await _saved_payload(db, project, user, saved)


@router.post("/projects/{project_id}/operations")
async def patch_project_spec(
    project_id: str,
    body: VideoOperationsWrite,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    project = await _manageable_project(db, project_id, user)
    try:
        spec, _lines = projects.apply_operations(dict(project.spec or {}), body.operations)
        saved = await projects.save_spec(db, user=user, raw=spec, project=project)
    except VideoSpecError as exc:
        raise _spec_error(exc) from None
    await db.commit()
    if saved.animations:
        await jobs.wake()
    await projects.queue_figure_stills(saved, user)
    return await _saved_payload(db, project, user, saved)


async def _saved_payload(db: AsyncSession, project: VideoProject, user: User, saved: projects.SavedSpec) -> dict:
    """The project after a save, with the scenes it left out of date and whether it wrote anything."""
    return {
        **await _project_payload(db, project, user),
        "scenes_changed_since_preview": saved.changed_since_preview,
        # What changed in each of them: ``narration``, ``overlay 0 quad``, ``transition in``.
        "scenes_changed_parts": saved.changed_parts,
        "scenes_restored_preview": saved.restored_preview,
        # The scenes whose passing preview still holds after the save: a render needs no new preview of them.
        "scenes_preview_holds": saved.preview_holds,
        "unchanged": saved.unchanged,
    }


@router.patch("/projects/{project_id}")
async def update_project(
    project_id: str,
    body: VideoProjectUpdate,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Who may open the video: people and groups with a tier, or the whole company (admins).

    The deck's one sharing write (``apply_sharing``): only a manager reaches
    it, publishing stays admin-only inside it, and a sharing change never
    bumps the revision. The accounts the call names and those already granted
    are locked before the video, in a user deletion's order.
    """
    from app.services.presentation_sharing import SHARING_FIELDS, apply_sharing, lock_for_sharing
    from app.services.share_principals import Principal, grant_snapshot, grants_audit

    project = await _manageable_project(db, project_id, user)
    if not SHARING_FIELDS & body.model_fields_set:
        return await _project_payload(db, project, user)
    named = [
        Principal("user", str(uid), "")
        for uid in (safe_uuid(str(value)) for value in (body.shared_user_ids or []))
        if uid is not None
    ]
    await lock_for_sharing(db, project, named)
    if not await projects.can_manage_project(db, project, user):
        raise HTTPException(status_code=403, detail="You can view this video but not change it.")
    before = grant_snapshot(project)
    await apply_sharing(db, user, project, body)
    await log_audit(
        db,
        user=user,
        action="update",
        entity_type="video_project",
        entity_id=str(project.id),
        entity_name=project.title,
        summary=f"Changed who can open video {project.title}",
        # Who gained, lost or changed access, as share_video logs it.
        details={
            "sharing_changed": True,
            "is_company_managed": bool(project.is_company_managed),
            **grants_audit(before, project),
        },
        request=request,
    )
    await db.commit()
    await db.refresh(project)
    return await _project_payload(db, project, user)


# ── Public link ───────────────────────────────────────────────────


async def _public_link_response(
    db: AsyncSession, *, token: str | None, created_at, created_by_user_id: uuid.UUID | None, allow_download: bool
) -> VideoPublicLinkResponse:
    from app.services.presentation_versions import resolve_user_names

    names = await resolve_user_names(db, [created_by_user_id]) if created_by_user_id else {}
    return VideoPublicLinkResponse(
        available=public_links.public_links_enabled(),
        enabled=True,
        path=public_links.public_link_path(token) if token else None,
        url=public_links.public_link_url(token) if token else None,
        created_at=created_at.isoformat() if created_at else None,
        created_by_name=names.get(str(created_by_user_id)),
        allow_download=allow_download,
    )


async def _stored_link_response(db: AsyncSession, link) -> VideoPublicLinkResponse:
    return await _public_link_response(
        db,
        token=public_links.link_token(link),
        created_at=link.created_at,
        created_by_user_id=link.created_by_user_id,
        allow_download=bool(link.allow_download),
    )


async def _audit_public_link(db: AsyncSession, user: User, project: VideoProject, outcome: str, request: Request) -> None:
    await log_audit(
        db,
        user=user,
        action="update",
        entity_type="video_project",
        entity_id=str(project.id),
        entity_name=project.title,
        summary=public_links.audit_summary(outcome, project.title),
        details=public_links.audit_details(outcome),
        request=request,
    )


@router.get("/projects/{project_id}/public-link", response_model=VideoPublicLinkResponse)
async def get_public_link(
    project_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> VideoPublicLinkResponse:
    """The video's public link, for whoever may share it (its managers)."""
    project = await _manageable_project(db, project_id, user)
    link = await public_links.load_public_link(db, project.id)
    if link is None:
        return VideoPublicLinkResponse(available=public_links.public_links_enabled(), enabled=False)
    return await _stored_link_response(db, link)


@router.post("/projects/{project_id}/public-link", response_model=VideoPublicLinkResponse)
async def enable_public_link(
    project_id: str,
    request: Request,
    body: VideoPublicLinkRequest | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> VideoPublicLinkResponse:
    """Turn the public link on, or hand back the one it has; ``regenerate`` issues a new token (403 while off)."""
    project = await _manageable_project(db, project_id, user)
    if not public_links.public_links_enabled():
        raise HTTPException(status_code=403, detail=public_links.PUBLIC_LINKS_OFF_DETAIL)
    change = await public_links.enable_public_link(
        db, project.id, actor_id=user.id, regenerate=bool(body and body.regenerate)
    )
    if change.changed:
        await _audit_public_link(db, user, project, change.outcome, request)
    await db.commit()
    return await _public_link_response(
        db,
        token=change.token,
        created_at=change.created_at,
        created_by_user_id=change.created_by_user_id,
        allow_download=change.allow_download,
    )


@router.patch("/projects/{project_id}/public-link", response_model=VideoPublicLinkResponse)
async def update_public_link(
    project_id: str,
    body: VideoPublicLinkUpdate,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> VideoPublicLinkResponse:
    """Whether the link's visitors may download the video; 404 for a video with no link."""
    project = await _manageable_project(db, project_id, user)
    if body.allow_download and not public_links.public_links_enabled():
        raise HTTPException(status_code=403, detail=public_links.PUBLIC_LINKS_OFF_DETAIL)
    changed = await public_links.set_download(db, project.id, body.allow_download)
    if changed is None:
        raise HTTPException(status_code=404, detail="This video has no public link")
    if changed:
        outcome = public_links.OUTCOME_DOWNLOAD_ON if body.allow_download else public_links.OUTCOME_DOWNLOAD_OFF
        await _audit_public_link(db, user, project, outcome, request)
    await db.commit()
    link = await public_links.load_public_link(db, project.id)
    if link is None:  # pragma: no cover - turned off between the commit and the read
        return VideoPublicLinkResponse(available=public_links.public_links_enabled(), enabled=False)
    return await _stored_link_response(db, link)


@router.delete("/projects/{project_id}/public-link", status_code=status.HTTP_204_NO_CONTENT)
async def disable_public_link(
    project_id: str, request: Request, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> Response:
    """Turn the public link off: its URL stops answering at once. Never refused, the admin switch off included."""
    from app.services.presentation_public_links import OUTCOME_DISABLED

    project = await _manageable_project(db, project_id, user)
    if await public_links.disable_public_link(db, project.id):
        await _audit_public_link(db, user, project, OUTCOME_DISABLED, request)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/projects/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(
    project_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> Response:
    # One path with the agent's delete_video: renders, previews, their files and figures go;
    # the recordings and AI shots stay in the library, their retention counted from now.
    from app.services.video_studio import deletion

    project = await _manageable_project(db, project_id, user)
    deleted = await deletion.delete_video(db, project, user)
    await db.commit()
    await deleted.finish()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ── Renders ───────────────────────────────────────────────────────────────


@router.post("/projects/{project_id}/renders", status_code=status.HTTP_202_ACCEPTED)
async def create_render(
    project_id: str,
    body: RenderRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
) -> dict:
    # A render becomes what the video plays (its cards, its public link) and
    # moves its renders through the retention: an edit, so its editors'.
    from app.services.video_studio.render_checks import RenderGateError

    project = await _manageable_project(db, project_id, user)
    if body.quality not in QUALITY_PRESETS:
        raise HTTPException(status_code=422, detail="Unknown quality.")
    try:
        started = await projects.start_render(
            db, project, user, quality=body.quality, force=body.force, ask_before_replacing=not body.replace
        )
    except projects.RenderLimitError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from None
    except projects.RenderStartBusyError as exc:
        raise HTTPException(status_code=503, detail=str(exc), headers={"Retry-After": "5"}) from None
    except RenderGateError as exc:
        # The quality gate: the editor lists the reasons and offers to render anyway (force).
        raise HTTPException(
            status_code=409,
            detail={"code": "render_gate", "message": str(exc), "reasons": [r.as_dict() for r in exc.reasons]},
        ) from None
    except projects.RenderReplaceAskedError as exc:
        # An older revision's render still in progress: the editor asks once, then starts with ``replace``.
        raise HTTPException(status_code=409, detail=exc.as_dict()) from None
    await db.commit()
    await jobs.wake()
    return {
        **(projects.serialize_render(started.render) or {}),
        "superseded_render_ids": [str(r.id) for r in started.superseded],
        # What it said of the renders it cancelled: an older revision's frames of its unchanged scenes kept.
        "superseded_line": projects.replaced_line(started.superseded, project.revision),
        # What the pre-render lint wrote before it started: a quad snapped to its bezel, saved.
        "notes": started.notes,
        # The render of this revision at this quality already in progress, answered instead of a twin.
        "reused": started.reused,
        # The render no worker resumed after its worker stopped, queued again as the same job from its kept
        # segments.
        "relaunched": started.relaunched,
        # The ready render that already covers this revision, answered with nothing queued: the revision it
        # was drawn for; ``force`` renders again.
        "covered_from": started.covered_from,
        # The ready render that drew this revision's scenes as they are now, drawn by an older release: why it
        # is drawn again (``drawn at screen-insert drawing version 7, now 8``).
        "redrawn": started.redrawn,
        # The scenes of the ready render answered that play a fallback take the voice pipeline or their lexicon
        # changed since: a re-check offered (render_video recheck), nothing queued; ``force`` renders again.
        "recheck": [
            {"scene_id": recheck.scene_id, "take": recheck.take.take(), "why": recheck.why, "fix": recheck.take.fix()}
            for recheck in started.recheck
        ],
        # How long it should take and the memory it needs against the worker's; ``None`` for a twin.
        "preflight": started.preflight.as_dict() if started.preflight is not None else None,
    }


@router.post("/projects/{project_id}/scene-renders", status_code=status.HTTP_202_ACCEPTED)
async def create_scene_render(
    project_id: str,
    body: SceneRenderRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
) -> dict:
    """A preview of one scene: its voice, music and transitions, in the preview lane.

    Drawn at ``quality`` - 720p30 or 1080p30; unset, 1080p for a scene
    showing a figure, a recording or a screen insert. Its editors',
    like a render: it speaks the narration and draws the frames. It never
    becomes what the video plays; ``GET /renders/{id}`` follows it. Every
    preview asked is queued in the preview lane, beside the whole
    renders, dealt round-robin by video; the answer's
    ``queue`` says where this one stands. It first passes the scene's
    pre-render lint: every static fault its preview would find,
    answered at once as the render gate's 409 (``force`` previews anyway).
    """
    from app.services.video_studio.render_checks import RenderGateError

    project = await _manageable_project(db, project_id, user)
    try:
        started = await projects.start_scene_render(
            db, project, user, scene=body.scene, quality=body.quality, force=body.force
        )
    except VideoSpecError as exc:
        raise _spec_error(exc) from None
    except projects.RenderStartBusyError as exc:
        raise HTTPException(status_code=503, detail=str(exc), headers={"Retry-After": "5"}) from None
    except RenderGateError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "render_gate", "message": str(exc), "reasons": [r.as_dict() for r in exc.reasons]},
        ) from None
    await db.commit()
    await projects.run_started(started)
    if getattr(started, "kept", None) is not None:
        # Served from a kept preview of the scene: answered as it is now.
        await db.refresh(started.render)
    return {
        **(projects.serialize_render(started.render) or {}),
        "superseded_render_ids": [str(r.id) for r in started.superseded],
        "notes": started.notes,
        # A source it draws sharp that a whole render at a larger quality would refuse as soft: advice.
        "advisories": started.advisories,
        # Where it stands in its lane of the render queue: ``position`` 1 is the next a free slot takes.
        "queue": started.queue.as_dict() if started.queue is not None else None,
    }


@router.get("/projects/{project_id}/renders")
async def list_renders(
    project_id: str,
    scene_previews: bool = Query(default=False, description="The video's scene previews instead of its renders."),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JobList:
    project = await _visible_project(db, project_id, user)
    kind = VideoRender.scene_id.is_not(None) if scene_previews else VideoRender.scene_id.is_(None)
    rows = list(
        (
            await db.execute(
                select(VideoRender)
                .where(VideoRender.project_id == project.id, kind)
                .order_by(VideoRender.created_at.desc())
                .limit(50)
            )
        )
        .scalars()
        .all()
    )
    held = await render_retention.standing(db, project)
    return JobList(items=[projects.serialize_render(r, project=project, standing=held) or {} for r in rows])


@router.post("/projects/{project_id}/renders/cleanup")
async def cleanup_renders(
    project_id: str,
    body: RenderCleanupRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Delete a video's renders by selector or by id: ``delete_video_renders``' rules and answer.

    ``dry_run`` answers the same list and deletes nothing (the Studio's preview
    of a cleanup). A render still in progress, the one the public link plays
    without ``confirm`` and, on a video the caller may only watch, someone
    else's render are refused by id; the rest go with their files.
    """
    project = await _visible_project(db, project_id, user)
    try:
        ask = render_cleanup.Ask.parse(**body.model_dump())
        outcome = await render_cleanup.run(db, project, user, ask)
    except render_cleanup.CleanupError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    if outcome.deleted:
        await log_audit(
            db,
            user=user,
            action="delete",
            entity_type="video_project",
            entity_id=str(project.id),
            entity_name=project.title,
            summary=render_cleanup.audit_summary(outcome),
            details=render_cleanup.audit_details(outcome),
            request=request,
        )
    await db.commit()
    await render_retention.purge(outcome.deletion)
    return outcome.as_dict()


async def _visible_render(db: AsyncSession, render_id: str, user: User) -> tuple[VideoRender, VideoProject]:
    render = await db.get(VideoRender, parse_path_id(render_id, not_found_detail=_NOT_FOUND))
    if render is None:
        raise _not_found()
    return render, await _visible_project(db, str(render.project_id), user)


@router.get("/renders/{render_id}")
async def get_render(
    render_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> dict:
    render, project = await _visible_render(db, render_id, user)
    return projects.serialize_render(render, project=project, standing=await render_retention.standing(db, project)) or {}


@router.post("/renders/{render_id}/cancel")
async def cancel_render(
    render_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> dict:
    render, project = await _visible_render(db, render_id, user)
    if render.user_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail="Only who asked for the render can cancel it.")
    await jobs.cancel(db, "render", render)
    await db.commit()
    return projects.serialize_render(render, project=project, standing=await render_retention.standing(db, project)) or {}


@router.patch("/renders/{render_id}")
async def update_render(
    render_id: str,
    body: RenderUpdate,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Keep a render out of the render retention, or make it the one the video plays; its managers."""
    render, project = await _visible_render(db, render_id, user)
    if not await projects.can_manage_project(db, project, user):
        raise HTTPException(status_code=403, detail="You can view this video but not change it.")
    # Locked before the video plays it, so the render retention never deletes it under the change.
    if body.current and not await render_retention.lock_render(db, render):
        raise _not_found()
    if body.current and (render.status != JOB_READY or render.file_id is None):
        raise HTTPException(status_code=409, detail="Only a finished render can be the one the video plays.")
    if body.current and render.scene_id:
        raise HTTPException(status_code=409, detail="A scene preview is never the one the video plays: render the video.")
    details: dict = {"render_id": str(render.id)}
    if body.keep is not None and bool(render.keep) != body.keep:
        render.keep = body.keep
        details["keep"] = body.keep
    if body.current and project.latest_render_id != render.id:
        project.latest_render_id = render.id
        details["current"] = True
    if len(details) > 1:
        await log_audit(
            db,
            user=user,
            action="update",
            entity_type="video_project",
            entity_id=str(project.id),
            entity_name=project.title,
            summary=(
                f"Made a {render.quality} render the current one of video {project.title}"
                if details.get("current")
                else f"{'Kept' if render.keep else 'Stopped keeping'} a render of video {project.title}"
            ),
            details=details,
            request=request,
        )
    await db.commit()
    await db.refresh(render)
    return projects.serialize_render(render, project=project, standing=await render_retention.standing(db, project)) or {}


@router.delete("/renders/{render_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_render(
    render_id: str, request: Request, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> Response:
    """Delete a render and its files: the video's managers, or who asked for it.

    One in progress is cancelled first. Deleting the current render makes the
    newest other finished one current, so the video, its chat card and its
    public link keep playing something when there is something to play. The
    render is locked before its video, as the render job locks them; its
    files and kept segments go through ``render_retention.delete``.
    """
    render, project = await _visible_render(db, render_id, user)
    if render.user_id != user.id and not await projects.can_manage_project(db, project, user):
        raise HTTPException(
            status_code=403, detail="Only the video's editors, or who asked for the render, can delete it."
        )
    if not await render_retention.lock_render(db, render):
        raise _not_found()
    await db.execute(select(VideoProject.id).where(VideoProject.id == project.id).with_for_update())
    await db.refresh(project)
    if render.status not in JOB_TERMINAL_STATUSES:
        await jobs.cancel(db, "render", render)
    if project.latest_render_id == render.id:
        project.latest_render_id = await render_retention.next_current(db, project, excluding=render.id)
    await log_audit(
        db,
        user=user,
        action="delete",
        entity_type="video_project",
        entity_id=str(project.id),
        entity_name=project.title,
        summary=f"Deleted a {render.quality} render of video {project.title}",
        details={"render_id": str(render.id), "revision": render.revision},
        request=request,
    )
    deletion = await render_retention.delete(db, [render])
    await db.commit()
    await render_retention.purge(deletion)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ── Recordings and clips ──────────────────────────────────────────────────


_ASSET_MODELS = {"take": VideoTake, "clip": VideoClip, "music": VideoMusic}


async def _readable_asset(db: AsyncSession, kind: str, asset_id: str, user: User):
    model = _ASSET_MODELS[kind]
    row = await db.get(model, parse_path_id(asset_id, not_found_detail=_NOT_FOUND))
    if row is None or not await projects.can_read_project_asset(db, kind, row, user):
        raise _not_found()
    return row


async def _owned_asset(db: AsyncSession, kind: str, asset_id: str, user: User):
    row = await _readable_asset(db, kind, asset_id, user)
    if row.user_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail="Only its creator can change it.")
    return row


def _id_list(value: str | None, label: str) -> list[uuid.UUID] | None:
    """Comma-separated ids, or ``None`` when the parameter is absent."""
    if value is None:
        return None
    return parse_uuid_list([part.strip() for part in value.split(",") if part.strip()], label)


@router.get("/assets")
async def list_assets(
    kind: library.AssetKind = "all",
    search: str | None = Query(default=None, max_length=200, description="Words of the name or the prompt."),
    ids: str | None = Query(default=None, max_length=4000, description="Comma-separated ids: only these."),
    exclude: str | None = Query(default=None, max_length=4000, description="Comma-separated ids to leave out."),
    available: bool = Query(default=False, description="Only the media whose files have not expired."),
    usage: library.AssetUsage = Query(
        default="all", description="`used`: some video uses it, anyone's; `unused`: none."
    ),
    date_from: datetime | None = Query(default=None, description="Made at or after this instant."),
    date_to: datetime | None = Query(default=None, description="Made at or before this instant."),
    limit: int = Query(default=library.ASSET_PAGE_SIZE, ge=1, le=library.MAX_PAGE_SIZE),
    offset: int = Query(default=0, ge=0),
    sort: library.AssetSort = Query(
        default="created_at", description="`created_at` (when made) or `title` (name, else prompt)."
    ),
    direction: library.SortDirection = Query(default="desc", description="`asc` or `desc`."),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> LibraryPage:
    """The caller's recordings and AI shots together, a page at a time, newest first unless *sort* / *direction* say otherwise.

    Each row carries its ``kind`` (``take`` / ``clip``), the videos the caller
    can open that use it (``used_in``) and whether any video uses it - theirs
    or not - which keeps it from expiring (``in_use``, ``expires_at`` null). ``ids``
    reads a video's own media whatever their age; ``exclude`` leaves them out
    of a library search, ``available`` the ones that can no longer be placed.
    ``usage`` reads ``in_use`` over the whole library, and ``date_from``
    / ``date_to`` bound when the media were made.
    """
    filters = library.AssetFilters(
        kind=kind,
        search=search,
        ids=_id_list(ids, "ids"),
        exclude=_id_list(exclude, "exclude") or [],
        available=available,
        usage=usage,
        created_from=date_from,
        created_to=date_to,
    )
    rows, total = await library.asset_page(
        db, user, filters, limit=limit, offset=offset, sort=sort, direction=direction
    )
    media = [(kind_of, row.id) for kind_of, row in rows]
    groups = await get_user_group_ids(db, user.id)
    used = await library.used_in(db, user, groups, media)
    in_use = await retention.media_in_use(db, media)
    items = []
    for kind_of, row in rows:
        serialize = jobs.serialize_take if kind_of == "take" else jobs.serialize_clip
        uses = used.get(str(row.id), [])
        used_now = str(row.id) in in_use
        items.append({"kind": kind_of, **serialize(row, in_use=used_now), "in_use": used_now, "used_in": uses})
    return LibraryPage(items=items, total=total)


@router.post("/assets/bulk-delete")
async def bulk_delete_assets(
    body: MediaBulkDelete,
    background: BackgroundTasks,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MediaBulkDeleteResult:
    """Delete the caller's media in bulk: the ``items`` named, or every one matching the library's filters.

    ``all_matching_filters`` takes the filters of ``GET /assets``. Only the
    caller's own media are deleted, as ``GET /assets`` lists them, and never
    one a video uses (``kept_in_use`` counts them): a single ``DELETE`` with
    ``force`` is the one way to remove such a medium. The use is read
    again as it deletes, and the storage objects are purged once it answered.
    """
    from app.services.file_cleanup import purge_storage_objects

    if body.all_matching_filters:
        filters = library.AssetFilters(
            kind=body.kind,
            search=body.search,
            usage=body.usage,
            created_from=body.date_from,
            created_to=body.date_to,
        )
        done = await library.delete_assets(db, user, filters=filters)
    elif body.items:
        named = [(item.kind, parse_uuid(item.id, label="media id")) for item in body.items]
        done = await library.delete_assets(db, user, items=named)
    else:
        raise HTTPException(status_code=400, detail="Name the media in items, or set all_matching_filters.")
    if done.count:
        await log_audit(
            db,
            user=user,
            action="delete",
            entity_type="video_media",
            summary=f"Deleted {done.count} media from the Video Studio library",
            details={
                "recordings": done.takes,
                "ai_shots": done.clips,
                "kept_in_use": done.kept,
                "by_filters": body.all_matching_filters,
                **({"usage": body.usage} if body.all_matching_filters else {}),
            },
        )
        await db.commit()
    if done.storage_keys:
        background.add_task(purge_storage_objects, done.storage_keys)
    return MediaBulkDeleteResult(deleted=done.count, kept_in_use=done.kept)


async def _update_asset(db: AsyncSession, kind: str, asset_id: str, body: MediaAssetUpdate, user: User):
    row = await _owned_asset(db, kind, asset_id, user)
    if body.name is not None:
        row.name = body.name.strip()[:200]
    if body.keep is not None and hasattr(row, "keep"):
        row.keep = body.keep
    await db.commit()
    await db.refresh(row)
    return row


@router.patch("/takes/{take_id}")
async def update_take(
    take_id: str, body: MediaAssetUpdate, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> dict:
    """Rename a recording, or keep it out of the retention (its owner)."""
    take = await _update_asset(db, "take", take_id, body, user)
    return jobs.serialize_take(take, in_use=await retention.in_use(db, take))


@router.get("/takes/{take_id}")
async def get_take(take_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    take = await _readable_asset(db, "take", take_id, user)
    return {
        **jobs.serialize_take(take, in_use=await retention.in_use(db, take)),
        "timeline": jobs.take_timeline(take),
        "script": take.script,
    }


@router.post("/takes/{take_id}/cancel")
async def cancel_take(take_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    take = await _owned_asset(db, "take", take_id, user)
    await jobs.cancel(db, "take", take, reason=jobs.cancelled_by(user, "the Video Studio"))
    await db.commit()
    return jobs.serialize_take(take)


_FORCE_DELETE_DESCRIPTION = "Delete it even though videos use it: their scenes are left without it."
_MEDIUM_AUDIT = {"take": ("video_take", "recording"), "clip": ("video_clip", "AI shot"), "music": ("video_music", "AI track")}


async def _clear_for_deletion(
    db: AsyncSession, kind: str, row: VideoTake | VideoClip | VideoMusic, user: User, *, force: bool, request: Request
) -> None:
    """Refuse deleting a medium a video uses unless *force* (409 ``media_in_use``); audit a forced one."""
    refusal = await library.deletion_refusal(db, user, kind, row.id)
    if refusal is None:
        return
    # A refusal force cannot lift (a repair made from the recording) stands under force too.
    if not force or refusal.get("force_allowed") is False:
        raise HTTPException(status_code=409, detail=refusal)
    entity_type, noun = _MEDIUM_AUDIT[kind]
    name = getattr(row, "name", None) or getattr(row, "prompt", None) or str(row.id)
    await log_audit(
        db,
        user=user,
        action="delete",
        entity_type=entity_type,
        entity_id=str(row.id),
        entity_name=name[:200],
        summary=f"Deleted {noun} {name[:80]} although videos use it",
        details={
            "forced": True,
            "videos": [video["id"] for video in refusal["videos"]],
            "other_videos": refusal["others"],
        },
        request=request,
    )


@router.delete("/takes/{take_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_take(
    take_id: str,
    request: Request,
    force: bool = Query(default=False, description=_FORCE_DELETE_DESCRIPTION),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    take = await _owned_asset(db, "take", take_id, user)
    await _clear_for_deletion(db, "take", take, user, force=force, request=request)
    files = await library.remove_media(db, [("take", take)])
    await db.commit()
    await assets.delete_files(files)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/clips/start-frame")
async def preview_clip_start_frame(
    reference_file_id: str | None = Query(default=None, max_length=64),
    reference_recording_id: str | None = Query(default=None, max_length=64),
    reference_clip_id: str | None = Query(default=None, max_length=64),
    reference_at_seconds: float | None = Query(default=None, ge=0),
    reference_at_step: int | None = Query(default=None, ge=0),
    aspect_ratio: Literal["16:9", "9:16"] = "16:9",
    crop: Literal["center"] | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ClipStartFramePreview:
    """The frame a shot asked with these ``reference_*`` arguments would open on, before anything is paid for.

    The checks and the frame of ``POST /clips`` (``clips.preview_start_frame``),
    saved as a file of the caller's and served by a media URL; a recording's
    page still loading is flagged with the moment it settles. Registered before
    ``/clips/{clip_id}``, which would read ``start-frame`` as an id.
    """
    from app.services.video_studio import clips as clip_providers

    try:
        plan = await clip_providers.plan_start_frame_preview(
            db,
            user,
            file_id=reference_file_id,
            recording_id=reference_recording_id,
            clip_id=reference_clip_id,
            at_seconds=reference_at_seconds,
            at_step=reference_at_step,
            aspect_ratio=aspect_ratio,
            crop=crop,
        )
        # No pooled connection is held while the video downloads and decodes.
        await db.commit()
        preview = await clip_providers.preview_start_frame(plan, user_id=user.id)
    except clip_providers.ClipError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    token, _expires = media.mint_media_token(uuid.UUID(preview.file_id), user.id)
    return ClipStartFramePreview(
        **preview.as_dict(), url=with_base_path(f"/api/v1/video-studio/media/{token}"), note=preview.note
    )


@router.post("/pictures/compose", status_code=status.HTTP_201_CREATED)
async def compose_picture(
    body: PictureCompose,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ComposedPicture:
    """Real pictures drawn inside a picture: ``compose_picture``'s composition, a file of the caller's.

    A base picture and 1-4 layers, each flat (``x``, ``y``, ``width``,
    ``height?``) or in perspective (``quad``), with ``opacity`` and ``fit``
    (``pictures.compose_files``: Pillow, no browser, no AI).
    """
    from app.services.video_studio import pictures

    try:
        png, (width, height) = await pictures.compose_files(db, user, body.base_file_id, body.layers)
    except pictures.PictureError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    await db.commit()
    row = await assets.store_bytes(
        png,
        filename=f"{(body.name or 'composed picture').strip()[:80]}.png",
        user_id=user.id,
        source=assets.SOURCE_UPLOAD,
        content_type="image/png",
    )
    token, _expires = media.mint_media_token(row.id, user.id)
    return ComposedPicture(
        file_id=str(row.id), url=with_base_path(f"/api/v1/video-studio/media/{token}"), width=width, height=height
    )


@router.post("/clips", status_code=status.HTTP_202_ACCEPTED)
async def create_clip(
    body: ClipCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
) -> dict:
    """An AI shot asked from the editor, placed in a video while it generates.

    The shot joins ``project_id`` as a clip scene (at ``index``, else at the
    end), or - ``new_video`` - starts a video of its own; the preview shows it
    as being generated until it is ready. The start is the agent tool's
    (``clips.start_clip``): same model resolution, same reference check, the
    art direction of the video it joins (a new video's: the default).
    """
    from app.services.video_studio import clips as clip_providers

    target = await _manageable_project(db, body.project_id, user) if body.project_id else None
    try:
        clip, _label = await clip_providers.start_clip(
            db,
            user,
            prompt=body.prompt,
            model=body.model,
            provider_id=body.provider_id,
            duration_seconds=body.duration_seconds,
            aspect_ratio=body.aspect_ratio,
            resolution=body.resolution,
            negative_prompt=body.negative_prompt,
            reference_file_id=body.reference_file_id,
            reference_recording_id=body.reference_recording_id,
            reference_clip_id=body.reference_clip_id,
            reference_at_seconds=body.reference_at_seconds,
            reference_at_step=body.reference_at_step,
            crop=body.crop,
            name=body.name,
            sound=body.sound,
            text_check=body.text_check,
            content_check=body.content_check,
            accept_minor_drift=body.accept_minor_drift,
            reference_image_file_ids=body.reference_image_file_ids,
            ingredients=[item.model_dump(exclude_none=True) for item in body.ingredients or []] or None,
            min_reference_similarity=body.min_reference_similarity,
            max_camera_motion=body.max_camera_motion,
            stabilise=body.stabilise,
            fallback_models=body.fallback_models,
            video_id=target.id if target is not None else None,
            screen=body.screen,
            keep_regions=body.keep_regions,
            stray_screens=body.stray_screens,
            insert=body.insert,
            ingredients_quality=body.ingredients_quality,
            reference_fit=body.reference_fit,
        )
    except clip_providers.ClipError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    scene = {"type": "clip", "clip_id": str(clip.id)}
    project = target
    try:
        if target is not None:
            operation = {"op": "insert_scene", "value": scene}
            if body.index is not None:
                operation["index"] = body.index
            spec, _lines = projects.apply_operations(dict(target.spec or {}), [operation])
            await projects.save_spec(db, user=user, raw=spec, project=target)
        elif body.new_video:
            title = (body.name or body.prompt).strip().splitlines()[0][:120] or "AI shot"
            project = (
                await _create_from_spec(
                    db, user, {"title": title, "scenes": [scene]}, summary="Created video {title} from an AI shot"
                )
            ).project
    except VideoSpecError as exc:
        raise _spec_error(exc) from None
    await log_audit(
        db,
        user=user,
        action="trigger",
        entity_type="video_project" if project is not None else "video_clip",
        entity_id=str(project.id) if project is not None else str(clip.id),
        entity_name=project.title if project is not None else (clip.name or clip.prompt[:80]),
        summary=f"Generated an AI shot with {clip.model}",
        details={"clip_id": str(clip.id), "model": clip.model},
    )
    await db.commit()
    await jobs.wake()
    return {
        "clip": jobs.serialize_clip(clip),
        "project": await _project_payload(db, project, user) if project is not None else None,
    }


@router.post("/clips/{clip_id}/retry", status_code=status.HTTP_202_ACCEPTED)
async def retry_clip(
    clip_id: str,
    body: ClipRetry,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
) -> dict:
    """Generate an AI shot again, in place - failed, expired, or ready but wrong: the videos using it keep it (its owner).

    With ``video_id``, under the art direction of that video's template. A ready shot keeps playing
    its take until the new one is ready; ``discard`` drops it now.
    """
    from app.services.video_studio import clips as clip_providers

    clip = await _owned_asset(db, "clip", clip_id, user)
    try:
        await clip_providers.restart_clip(db, user, clip, **body.model_dump(exclude_none=True))
    except clip_providers.ClipError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    await log_audit(
        db,
        user=user,
        action="trigger",
        entity_type="video_clip",
        entity_id=str(clip.id),
        entity_name=clip.name or clip.prompt[:80],
        summary=f"Generated an AI shot again with {clip.model}",
        details={"clip_id": str(clip.id), "model": clip.model, "attempt": (clip.params or {}).get("attempt")},
    )
    await db.commit()
    await jobs.wake()
    return jobs.serialize_clip(clip)


@router.patch("/clips/{clip_id}")
async def update_clip(
    clip_id: str, body: MediaAssetUpdate, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> dict:
    """Rename an AI shot, or keep it out of the retention (its owner)."""
    clip = await _update_asset(db, "clip", clip_id, body, user)
    return jobs.serialize_clip(clip, in_use=await retention.in_use(db, clip))


@router.get("/clips/{clip_id}")
async def get_clip(clip_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    clip = await _readable_asset(db, "clip", clip_id, user)
    return jobs.serialize_clip(clip, in_use=await retention.in_use(db, clip))


@router.delete("/clips/{clip_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_clip(
    clip_id: str,
    request: Request,
    force: bool = Query(default=False, description=_FORCE_DELETE_DESCRIPTION),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    clip = await _owned_asset(db, "clip", clip_id, user)
    await _clear_for_deletion(db, "clip", clip, user, force=force, request=request)
    files = await library.remove_media(db, [("clip", clip)])
    await db.commit()
    await assets.delete_files(files)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ── Music ─────────────────────────────────────────────────────────


@router.get("/music/library")
async def music_library(user: User = Depends(get_current_user)) -> JobList:
    """The built-in tracks, each with a URL to listen to half a minute of its arc."""
    items = [
        {**track, "audition_url": preview.music_bed_url(music_tracks.audition_bed(track["id"]), user)}
        for track in music_tracks.library()
    ]
    return JobList(items=items)


@router.get("/music")
async def list_music(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> JobList:
    """The caller's AI tracks, newest first, with the videos playing them."""
    rows = (
        (
            await db.execute(
                select(VideoMusic).where(VideoMusic.user_id == user.id).order_by(VideoMusic.created_at.desc()).limit(200)
            )
        )
        .scalars()
        .all()
    )
    groups = await get_user_group_ids(db, user.id)
    used = await library.used_in(db, user, groups, [("music", m.id) for m in rows])
    return JobList(items=[{**music_tracks.serialize_music(m), "used_in": used.get(str(m.id), [])} for m in rows])


@router.post("/music", status_code=status.HTTP_202_ACCEPTED)
async def create_music(
    body: MusicCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
) -> dict:
    """An AI track asked from the editor; with ``project_id``, composed at the video's length and its music.

    The start is the agent tool's (``music.start_music``): same model
    resolution, same parameters. The video keeps its levels and plays the
    track once it is composed (the preview says it is being composed).
    """
    target = await _manageable_project(db, body.project_id, user) if body.project_id else None
    seconds = body.duration_seconds
    if seconds is None:
        planned = (target.estimated_duration_ms or 60_000) / 1000 if target is not None else 60.0
        rendered = await projects.rendered_seconds(db, target) if target is not None else None
        seconds = music_tracks.fitted_seconds(music_tracks.video_seconds(planned, rendered))
    try:
        row, _label = await music_tracks.start_music(
            db,
            user,
            prompt=body.prompt,
            duration_seconds=seconds,
            model=body.model,
            provider_id=body.provider_id,
            instrumental=body.instrumental,
            negative_prompt=body.negative_prompt,
            name=body.name,
        )
    except music_tracks.MusicError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    if target is not None:
        spec = dict(target.spec or {})
        bed = {k: v for k, v in dict(spec.get("music") or {}).items() if k in _MUSIC_LEVELS}
        spec["music"] = {**bed, "music_id": str(row.id)}
        try:
            await projects.save_spec(db, user=user, raw=spec, project=target)
        except VideoSpecError as exc:
            raise _spec_error(exc) from None
    await log_audit(
        db,
        user=user,
        action="trigger",
        entity_type="video_project" if target is not None else "video_music",
        entity_id=str(target.id) if target is not None else str(row.id),
        entity_name=target.title if target is not None else (row.name or row.prompt[:80]),
        summary=f"Composed an AI track with {row.model}",
        details={"music_id": str(row.id), "model": row.model, "seconds": seconds},
    )
    await db.commit()
    await jobs.wake()
    return {
        "music": music_tracks.serialize_music(row),
        "project": await _project_payload(db, target, user) if target is not None else None,
    }


#: What a video's music keeps when it changes source: how it sits, not what plays.
_MUSIC_LEVELS = frozenset({"volume", "ducking", "duck_db", "start", "loop", "ending", "fade_in", "fade_out"})


@router.post("/music/{music_id}/retry", status_code=status.HTTP_202_ACCEPTED)
async def retry_music(
    music_id: str,
    body: MusicRetry,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
) -> dict:
    """Generate a failed AI track again, in place: the videos using it keep it (its owner)."""
    row = await _owned_asset(db, "music", music_id, user)
    try:
        await music_tracks.restart_music(db, user, row, **body.model_dump(exclude_none=True))
    except music_tracks.MusicError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    await log_audit(
        db,
        user=user,
        action="trigger",
        entity_type="video_music",
        entity_id=str(row.id),
        entity_name=row.name or row.prompt[:80],
        summary=f"Composed an AI track again with {row.model}",
        details={"music_id": str(row.id), "model": row.model, "attempt": (row.params or {}).get("attempt")},
    )
    await db.commit()
    await jobs.wake()
    return music_tracks.serialize_music(row)


@router.patch("/music/{music_id}")
async def update_music(
    music_id: str, body: MediaAssetUpdate, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> dict:
    """Rename an AI track (its owner)."""
    return music_tracks.serialize_music(await _update_asset(db, "music", music_id, body, user))


@router.get("/music/{music_id}")
async def get_music(music_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    return music_tracks.serialize_music(await _readable_asset(db, "music", music_id, user))


@router.delete("/music/{music_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_music(
    music_id: str,
    request: Request,
    force: bool = Query(default=False, description=_FORCE_DELETE_DESCRIPTION),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    row = await _owned_asset(db, "music", music_id, user)
    await _clear_for_deletion(db, "music", row, user, force=force, request=request)
    files = music_tracks.held_files(row)
    await jobs.cancel(db, "music", row)
    await db.delete(row)
    await db.commit()
    await assets.delete_files(files)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/music-bed/{token}")
async def stream_music_bed(token: str) -> Response:
    """A music bed as the editor and the library play it (AAC, byte ranges).

    No auth dependency: ``<audio>`` sends no session header, and the token,
    minted for the reader after the checks of the preview or the library,
    is the credential. What the bed is made of is checked again for that
    reader; nothing holds a database session while it is prepared.
    """
    from fastapi.responses import FileResponse

    from app.services.file_access import can_read_file
    from app.services.video_studio.score import get_style

    try:
        claims, user_id = media.decode_music_token(token)
        bed = music_tracks.Bed.from_claims(claims)
    except (media.MediaTokenError, KeyError, TypeError, ValueError):
        raise _not_found() from None
    file_id = None
    async with async_session() as db:
        user = await db.get(User, user_id)
        if user is None:
            raise _not_found()
        if bed.kind == "track":
            if get_style(bed.ref) is None:
                raise _not_found()
        elif bed.kind == "music":
            row = await db.get(VideoMusic, parse_path_id(bed.ref, not_found_detail=_NOT_FOUND))
            if row is None or not await projects.can_read_project_asset(db, "music", row, user) or not row.file_id:
                raise _not_found()
            file_id = row.file_id
        else:
            row = await db.get(FileAttachment, parse_path_id(bed.ref, not_found_detail=_NOT_FOUND))
            if row is None or not await can_read_file(row, user, db):
                raise _not_found()
            file_id = row.id
    try:
        path = await music_tracks.preview_bed(bed, file_id)
    except (RuntimeError, encoder.EncoderError) as exc:
        raise HTTPException(status_code=503, detail=f"The music could not be prepared: {exc}"[:300]) from None
    return FileResponse(
        path,
        media_type="audio/mp4",
        headers={"Cache-Control": "private, max-age=2700", "X-Content-Type-Options": "nosniff"},
    )


# ── The render retention ──────────────────────────────────────────


@router.get("/render-retention")
async def get_render_retention(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    """The days a render nothing else keeps is kept after it ended (``render_retention.Policy``), and who may change them."""
    return {**(await render_retention.load_policy(db)).as_dict(), "can_edit": bool(user.is_admin)}


@router.put("/render-retention")
async def save_render_retention(
    body: RenderRetentionWrite,
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Change the render retention (administrators): the rules given, each in days, 0 keeping renders for good."""
    before, after = await render_retention.save_policy(db, body.model_dump(exclude_none=True))
    was, now = before.as_dict(), after.as_dict()
    changed = {rule: {"from": was[rule], "to": now[rule]} for rule in now if was[rule] != now[rule]}
    if changed:
        await log_audit(
            db,
            user=user,
            action="configure",
            entity_type="platform_settings",
            entity_name="Video Studio render retention",
            summary="Changed the Video Studio render retention",
            details={"render_retention": changed},
            request=request,
        )
    await db.commit()
    return {**now, "can_edit": True}


# ── The pronunciation lexicon ─────────────────────────────────────


@router.get("/pronunciation-lexicon")
async def get_pronunciation_lexicon(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    """The platform's pronunciation lexicon, its newest version: what every video's voice-over reads under its own."""
    from app.services.video_studio import lexicon_store

    current = await lexicon_store.current(db)
    return {**current.as_dict(), "can_edit": bool(user.is_admin)}


@router.put("/pronunciation-lexicon")
async def save_pronunciation_lexicon(
    body: PronunciationLexiconWrite, user: User = Depends(require_admin), db: AsyncSession = Depends(get_db)
) -> dict:
    """A new version of the platform's lexicon (administrators): every entry validated, one per written form."""
    from app.services.video_studio import lexicon_store

    try:
        saved = await lexicon_store.save(db, body.entries, user=user, note=body.note)
    except lexicon_store.LexiconError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    await log_audit(
        db,
        user=user,
        action="update",
        entity_type="video_pronunciation_lexicon",
        entity_id=saved.id,
        entity_name=f"Pronunciation lexicon v{saved.version}",
        summary=f"Saved version {saved.version} of the pronunciation lexicon ({len(saved.entries)} entries)",
        details={"note": saved.note} if saved.note else None,
    )
    await db.commit()
    return {**saved.as_dict(), "can_edit": True}


@router.get("/pronunciation-lexicon/versions")
async def list_pronunciation_lexicon_versions(
    _user: User = Depends(require_admin), db: AsyncSession = Depends(get_db)
) -> dict:
    """Every version of the platform's lexicon, newest first (administrators)."""
    from app.services.video_studio import lexicon_store

    return {"items": await lexicon_store.versions(db)}


@router.get("/pronunciation-lexicon/versions/{version}")
async def get_pronunciation_lexicon_version(
    version: int, _user: User = Depends(require_admin), db: AsyncSession = Depends(get_db)
) -> dict:
    """One version of the platform's lexicon, its entries (administrators)."""
    from app.services.video_studio import lexicon_store

    found = await lexicon_store.version(db, version)
    if found is None:
        raise _not_found()
    return found.as_dict()


@router.post("/pronunciation-lexicon/versions/{version}/restore")
async def restore_pronunciation_lexicon_version(
    version: int, user: User = Depends(require_admin), db: AsyncSession = Depends(get_db)
) -> dict:
    """A new version holding an older one's entries (administrators): nothing is written over."""
    from app.services.video_studio import lexicon_store

    restored = await lexicon_store.restore(db, version, user=user)
    if restored is None:
        raise _not_found()
    await log_audit(
        db,
        user=user,
        action="update",
        entity_type="video_pronunciation_lexicon",
        entity_id=restored.id,
        entity_name=f"Pronunciation lexicon v{restored.version}",
        summary=f"Restored version {version} of the pronunciation lexicon as version {restored.version}",
    )
    await db.commit()
    return {**restored.as_dict(), "can_edit": True}


@router.post("/pronunciations/speak")
async def speak_pronunciation(
    body: PronunciationSpeakRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
) -> dict:
    """One lexicon term spoken as a video's voice-over says it: the editor's play button next to each term.

    Through the same lexicon, mechanism and voice cache as a narration (spoken
    once per reading), played through a voice token like the preview's.
    """
    import tempfile

    from pydantic import ValidationError

    from app.core.utils import format_exception_message
    from app.services.video_studio import lexicon_store, voice
    from app.services.video_studio.spec import Voiceover

    try:
        voiceover = Voiceover.model_validate(body.voiceover or {})
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid voice-over: {exc.errors()[0].get('msg')}") from None
    try:
        target = voice.detached(await voice.resolve(db, user.id, voiceover))
    except voice.VoiceUnavailableError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    lexicon = await lexicon_store.for_voiceover(db, voiceover)
    reader = user.id
    # Nothing is written: the session's transaction ends before the speech model is asked.
    await db.rollback()
    line = voice.Line("pronunciation", " ".join(body.text.split()), None, None)
    digest = voice.digest_of(target, line, voiceover, lexicon)
    rendition = voice.rendition_of(target, line, voiceover, lexicon)
    seconds = await voice.spoken_seconds(digest)
    if seconds is None:
        with tempfile.TemporaryDirectory(prefix="xtm-voice-") as work:
            try:
                await voice.synthesize(
                    target,
                    text=line.text,
                    voice=voiceover.voice,
                    style=voiceover.style,
                    speed=voiceover.speed,
                    out_path=f"{work}/voice.wav",
                    lexicon=lexicon,
                )
            except Exception as exc:
                raise HTTPException(
                    status_code=502, detail=f"The term could not be spoken: {format_exception_message(exc)}"[:300]
                ) from None
        seconds = await voice.spoken_seconds(digest)
    token, _expires = media.mint_voice_token(digest, reader)
    return {
        "text": line.text,
        "audio_url": with_base_path(f"/api/v1/video-studio/voice/{token}"),
        "seconds": seconds,
        "mechanism": rendition.mechanism,
        "sent": rendition.text,
        "lexicon_version": lexicon.version,
    }


@router.get("/voice/{token}")
async def stream_voice(token: str) -> Response:
    """A narration the editor's preview plays (AAC, byte ranges), spoken already.

    No auth dependency, like the music bed: the token, minted for the reader
    by the preview that planned the narration, is the credential, and names
    a narration in the voice cache by its digest. Nothing is spoken here.
    """
    from fastapi.responses import FileResponse

    from app.services.video_studio import voice

    try:
        digest, user_id = media.decode_voice_token(token)
    except media.MediaTokenError:
        raise _not_found() from None
    async with async_session() as db:
        if await db.get(User, user_id) is None:
            raise _not_found()
    try:
        path = await voice.preview_audio(digest)
    except encoder.EncoderError as exc:
        raise HTTPException(status_code=503, detail=f"The narration could not be prepared: {exc}"[:300]) from None
    if path is None:
        raise _not_found()
    return FileResponse(
        path,
        media_type="audio/mp4",
        headers={"Cache-Control": "private, max-age=2700", "X-Content-Type-Options": "nosniff"},
    )


@router.get("/figure-preview/{token}")
async def stream_figure_preview(token: str) -> Response:
    """The still of a figure the editor's preview shows, drawn when the video was saved.

    No auth dependency, like the voice: the token, minted for the reader by
    the preview that resolved the figure as them (its deck, its pictures), is
    the credential, and names a still by the digest of what it shows.
    """
    from app.services.file_storage import FileNotFoundInStorageError, file_storage
    from app.services.video_studio import figures

    try:
        digest, user_id = media.decode_figure_token(token)
    except media.MediaTokenError:
        raise _not_found() from None
    async with async_session() as db:
        if await db.get(User, user_id) is None:
            raise _not_found()
    try:
        data = await file_storage.download(figures.preview_object(digest))
    except FileNotFoundInStorageError:
        raise _not_found() from None
    return Response(
        content=data,
        media_type="image/jpeg",
        headers={"Cache-Control": "private, max-age=2700", "X-Content-Type-Options": "nosniff"},
    )


@router.get("/figure-build/{token}")
async def stream_figure_build(token: str, request: Request) -> Response:
    """The build of a figure the editor's stage plays where its scene plays, by ranges.

    The figure token of its still names it: minted for the reader by the
    preview that resolved the figure as them, it names a build by the digest
    of the still and of the scene's timing (``figures.build_digest``).
    """
    from types import SimpleNamespace

    from app.services.file_storage import FileNotFoundInStorageError, file_storage
    from app.services.video_studio import figures

    try:
        digest, user_id = media.decode_figure_token(token)
    except media.MediaTokenError:
        raise _not_found() from None
    async with async_session() as db:
        if await db.get(User, user_id) is None:
            raise _not_found()
    stored = await file_storage.stat(figures.build_object(digest))
    if stored is None:
        raise _not_found()
    row = SimpleNamespace(size=stored["size"], content_type="video/mp4", s3_object_name=figures.build_object(digest))
    headers = {"Content-Type": "video/mp4", "Cache-Control": "private, max-age=2700", "X-Content-Type-Options": "nosniff"}
    try:
        return await media.serve_file(row, request.headers.get("range"), headers)
    except FileNotFoundInStorageError:
        raise _not_found() from None


@router.get("/insert-build/{token}")
async def stream_insert_build(token: str, request: Request) -> Response:
    """A screen insert as the editor's stage plays it: its build, drawn by the render's compositor, by ranges.

    No auth dependency, like a figure's build: the insert token, minted for
    the reader by the preview that resolved the insert's files as them, is
    the credential, and names the build by its digest.
    """
    from types import SimpleNamespace

    from app.services.file_storage import FileNotFoundInStorageError, file_storage
    from app.services.video_studio import insert_builds

    try:
        digest, user_id = media.decode_insert_token(token)
    except media.MediaTokenError:
        raise _not_found() from None
    async with async_session() as db:
        if await db.get(User, user_id) is None:
            raise _not_found()
    stored = await file_storage.stat(insert_builds.build_object(digest))
    if stored is None:
        raise _not_found()
    row = SimpleNamespace(size=stored["size"], content_type="video/webm", s3_object_name=insert_builds.build_object(digest))
    headers = {"Content-Type": "video/webm", "Cache-Control": "private, max-age=2700", "X-Content-Type-Options": "nosniff"}
    try:
        return await media.serve_file(row, request.headers.get("range"), headers)
    except FileNotFoundInStorageError:
        raise _not_found() from None


# ── Media ─────────────────────────────────────────────────────────────────


async def _media_file(db: AsyncSession, body: MediaTokenRequest, user: User) -> tuple[uuid.UUID | None, str | None]:
    """The file a token is asked for, and the name a download saves it under."""
    if body.kind == "render":
        render, project = await _visible_render(db, body.id, user)
        # A scene preview's frames, in order.
        frames = dict(zip(("first_frame", "mid_frame", "last_frame"), (safe_uuid(str(f)) for f in render.frame_file_ids or []), strict=False))
        file_id = {
            "video": render.file_id,
            # A scene preview stores no poster: its first settled frame stands for it, so the player never asks
            # for a poster that is not there.
            "poster": render.poster_file_id or frames.get("first_frame"),
            "captions": render.captions_file_id,
            **frames,
        }.get(body.asset)
        return file_id, media.download_filename(project.title, body.asset, height=render.height, fps=render.fps)
    if body.kind == "take":
        take = await _readable_asset(db, "take", body.id, user)
        file_id = {"video": take.file_id, "poster": take.poster_file_id, "contact_sheet": take.contact_sheet_file_id}
        return file_id.get(body.asset), media.download_filename(take.name, body.asset)
    if body.kind == "clip":
        from app.services.video_studio import frame_qc
        from app.services.video_studio.clips import start_frame_file_id

        clip = await _readable_asset(db, "clip", body.id, user)
        started = safe_uuid(start_frame_file_id(clip.params) or "")
        kept = {name.removesuffix("_file_id"): safe_uuid(fid or "") for name, fid in frame_qc.summary(clip.params).items() if name.endswith("_file_id")}
        file_id = {"video": clip.file_id, "poster": clip.poster_file_id, "start_frame": started, **kept}.get(body.asset)
        return file_id, media.download_filename(clip.name or clip.prompt[:80], body.asset)
    track = await _readable_asset(db, "music", body.id, user)
    return (track.file_id if body.asset == "audio" else None), None


#: What the editor uploads into a video: a picture (an image scene, a logo, a
#: shot's first frame), a sound (the music bed), a video file (a clip scene).
_UPLOAD_KINDS = ("image/", "audio/", "video/")


@router.post("/files", status_code=status.HTTP_201_CREATED)
async def upload_file(
    file: UploadFile = File(...), user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> dict:
    """A file for a video, the caller's own: a spec names it by ``file_id``."""
    from app.services.file_storage import MAX_UPLOAD_SIZE_BYTES, guess_content_type

    filename = (file.filename or "upload").strip()[:255] or "upload"
    content_type = file.content_type or guess_content_type(filename)
    if not content_type.startswith(_UPLOAD_KINDS):
        raise HTTPException(status_code=415, detail="A video takes images, sounds and video files.")
    data = await file.read()
    if len(data) > MAX_UPLOAD_SIZE_BYTES:
        raise HTTPException(
            status_code=413, detail=f"File too large. Maximum is {MAX_UPLOAD_SIZE_BYTES // (1024 * 1024)} MB."
        )
    row = await assets.store_bytes(
        data, filename=filename, user_id=user.id, source=assets.SOURCE_UPLOAD, content_type=content_type
    )
    return {"file_id": str(row.id), "filename": filename, "content_type": content_type, "size": len(data)}


@router.post("/media-token")
async def media_token(
    body: MediaTokenRequest, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> MediaTokenResponse:
    file_id, filename = await _media_file(db, body, user)
    row = await db.get(FileAttachment, file_id) if file_id else None
    if row is None:
        raise _not_found()
    token, expires = media.mint_media_token(row.id, user.id, filename=filename if body.download else None)
    return MediaTokenResponse(
        url=with_base_path(f"/api/v1/video-studio/media/{token}"), expires_at=expires, content_type=row.content_type
    )


@router.get("/media/{token}")
async def stream_media(token: str, request: Request, db: AsyncSession = Depends(get_db)) -> Response:
    """The bytes a media token names: played by ranges, or saved under its download name."""
    from app.services.file_responses import file_response_headers, token_max_age
    from app.services.file_storage import FileNotFoundInStorageError

    try:
        claims = media.decode_media_token(token)
    except media.MediaTokenError:
        raise _not_found() from None
    row = await db.get(FileAttachment, claims.file_id)
    if row is None:
        raise _not_found()
    headers = file_response_headers(row, max_age=token_max_age(claims.expires))
    if claims.filename:
        headers["Content-Disposition"] = build_content_disposition(claims.filename)
    try:
        return await media.serve_file(row, request.headers.get("range"), headers)
    except FileNotFoundInStorageError:
        raise _not_found() from None


# ── Jobs ──────────────────────────────────────────────────────────────────


@router.get("/active")
async def active_jobs(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    """The person's recordings, clips and renders that have not finished, for the page's live view."""
    takes = (
        (
            await db.execute(
                select(VideoTake).where(VideoTake.user_id == user.id, VideoTake.status.not_in(JOB_TERMINAL_STATUSES))
            )
        )
        .scalars()
        .all()
    )
    clips = (
        (
            await db.execute(
                select(VideoClip).where(VideoClip.user_id == user.id, VideoClip.status.not_in(JOB_TERMINAL_STATUSES))
            )
        )
        .scalars()
        .all()
    )
    renders = (
        (
            await db.execute(
                select(VideoRender).where(
                    VideoRender.user_id == user.id, VideoRender.status.not_in(JOB_TERMINAL_STATUSES)
                )
            )
        )
        .scalars()
        .all()
    )
    tracks = (
        (
            await db.execute(
                select(VideoMusic).where(VideoMusic.user_id == user.id, VideoMusic.status.not_in(JOB_TERMINAL_STATUSES))
            )
        )
        .scalars()
        .all()
    )
    return {
        "takes": [jobs.serialize_take(t) for t in takes],
        "clips": [jobs.serialize_clip(c) for c in clips],
        "renders": [projects.serialize_render(r) for r in renders],
        "music": [music_tracks.serialize_music(m) for m in tracks],
    }
