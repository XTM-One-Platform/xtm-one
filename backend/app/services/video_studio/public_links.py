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

"""The public link of a video: anyone with the URL may watch it.

Every other way into a video goes through its grants (``user_visible_filter``);
a public link is the one exception, on purpose: a capability URL the video's
managers hand to people who have no account here - a customer, a partner, an
event audience. It is the deck's public link (``presentation_public_links``)
for a video, and it shares that module's token idiom (:func:`mint_token`,
:func:`well_formed_token`) and its tool vocabulary. Two surfaces change it, both
through this module, once their caller checked the user may manage the video:
the Share dialog (``GET`` / ``POST`` / ``PATCH`` / ``DELETE
/video-studio/projects/{id}/public-link``) and the ``set_video_public_link``
built-in tool.

* **The token** is 256 bits, looked up by its SHA-256 and kept
  Fernet-encrypted (invariant 2) so a manager can copy the link again. One
  link per video; ``regenerate`` rewrites the token in place and the old URL
  stops answering at once; ``disable`` deletes the row.
* **The admin switch** ``video_public_links_enabled`` (Settings > Access and security) is
  read on EVERY public request: off, every link answers like a token nobody
  issued, and none can be created. The rows are kept. The ``VIDEO_STUDIO``
  feature flag off reads the same.
* **What a link shows** is the video's CURRENT render (``latest_render_id``)
  and nothing else: its MP4, poster and captions, the title, the length and
  the size - never the spec, the recordings, the owner or who it is shared
  with (:func:`public_video_payload`, whitelisted by ``PublicVideoResponse``).
  A new render becomes what the link plays. ``allow_download`` (off on a new
  link) adds the download of the MP4 and its captions.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from cryptography.fernet import InvalidToken
from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.feature_flags import is_feature_enabled
from app.core.security import decrypt_value, encrypt_value, hash_token
from app.core.security_options import security_option
from app.models.video_studio import JOB_READY, VideoProject, VideoPublicLink, VideoRender
from app.services.presentation_public_links import (
    OUTCOME_CREATED,
    OUTCOME_DISABLED,
    OUTCOME_KEPT,
    OUTCOME_REGENERATED,
    mint_token,
    well_formed_token,
)

logger = logging.getLogger(__name__)

#: The web app page a public link opens (``VideoPublic.tsx``).
PUBLIC_LINK_ROUTE = "/public/videos"

#: The answer while the admin switch is off, for the REST surface and the tool.
PUBLIC_LINKS_OFF_DETAIL = (
    "Public video links are turned off on this platform by an administrator (Settings > Access and security > Public video links)."
)

#: The audit ``details.public_link`` of the download option's changes.
OUTCOME_DOWNLOAD_ON = "download_enabled"
OUTCOME_DOWNLOAD_OFF = "download_disabled"

#: The files a link serves of the current render, by the name the route takes.
PUBLIC_ASSETS: tuple[str, ...] = ("video", "poster", "captions")
#: The ones a download may fetch as an attachment, once the link allows it.
DOWNLOADABLE_ASSETS: frozenset[str] = frozenset({"video", "captions"})


def public_links_enabled() -> bool:
    """Whether public video links answer and may be created: the Video Studio on and the admin switch."""
    return is_feature_enabled("VIDEO_STUDIO") and bool(security_option("video_public_links_enabled"))


def public_link_path(token: str) -> str:
    """The root-relative page address (the web app adds its base path)."""
    return f"{PUBLIC_LINK_ROUTE}/{token}"


def public_link_url(token: str) -> str:
    """The absolute address a person pastes into a message."""
    from app.services.email import platform_url

    return f"{platform_url()}{public_link_path(token)}"


def link_token(link: VideoPublicLink) -> str | None:
    """The token of a stored link, or ``None`` when ``SECRET_KEY`` no longer decrypts it."""
    try:
        return decrypt_value(link.token_enc)
    except (InvalidToken, ValueError):
        logger.warning("The public link of video %s can no longer be decrypted", link.project_id)
        return None


# ── Reads ─────────────────────────────────────────────────────────────────────


async def load_public_link(db: AsyncSession, project_id: uuid.UUID) -> VideoPublicLink | None:
    result = await db.execute(select(VideoPublicLink).where(VideoPublicLink.project_id == project_id))
    return result.scalar_one_or_none()


async def public_video_ids(db: AsyncSession, project_ids: Iterable[uuid.UUID]) -> set[str]:
    """Which of *project_ids* a public link opens now; empty while the admin switch is off."""
    ids = list(project_ids)
    if not ids or not public_links_enabled():
        return set()
    rows = await db.execute(select(VideoPublicLink.project_id).where(VideoPublicLink.project_id.in_(ids)))
    return {str(project_id) for project_id in rows.scalars().all()}


async def resolve_public_link(db: AsyncSession, token: object) -> tuple[VideoProject, VideoPublicLink] | None:
    """The video *token* opens and its link, or ``None`` - one answer for every refusal.

    An unknown, regenerated or disabled token, a deleted video and the admin
    switch off all read the same, so a caller never tells a dead link from
    one that never existed.
    """
    if not public_links_enabled() or not well_formed_token(token):
        return None
    result = await db.execute(
        select(VideoProject, VideoPublicLink)
        .join(VideoPublicLink, VideoPublicLink.project_id == VideoProject.id)
        .where(VideoPublicLink.token_hash == hash_token(str(token)))
    )
    row = result.first()
    return (row[0], row[1]) if row is not None else None


async def current_render(db: AsyncSession, project: VideoProject) -> VideoRender | None:
    """The render a link plays: the video's current render, when it is ready and has its MP4."""
    if project.latest_render_id is None:
        return None
    render = await db.get(VideoRender, project.latest_render_id)
    if render is None or render.status != JOB_READY or render.file_id is None:
        return None
    return render


def public_file_id(render: VideoRender | None, asset: str) -> uuid.UUID | None:
    """The file of *render* a public route may serve as *asset*, or ``None``."""
    if render is None or asset not in PUBLIC_ASSETS:
        return None
    return {"video": render.file_id, "poster": render.poster_file_id, "captions": render.captions_file_id}[asset]


def public_video_payload(project: VideoProject, render: VideoRender | None, link: VideoPublicLink) -> dict[str, Any]:
    """The video as its public link shows it, for ``PublicVideoResponse``: no owner, spec or grant."""
    download = bool(link.allow_download) and render is not None
    return {
        "title": project.title,
        "rendered": render is not None,
        "duration_ms": render.duration_ms if render else None,
        "width": render.width if render else None,
        "height": render.height if render else None,
        "fps": render.fps if render else None,
        "has_poster": bool(render and render.poster_file_id),
        "has_captions": bool(render and render.captions_file_id),
        "captions_burned_in": bool(render) and render.captions_burned_in is not False,
        "rendered_at": render.finished_at.isoformat() if render and render.finished_at else None,
        "download": download,
        "size_bytes": render.size_bytes if download else None,
        "chapters": list(render.chapters or []) if render else [],
    }


# ── Writes ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PublicLinkChange:
    """What :func:`enable_public_link` did, and the link as it stands."""

    outcome: str  # OUTCOME_CREATED | OUTCOME_REGENERATED | OUTCOME_KEPT
    token: str | None
    created_at: datetime | None
    created_by_user_id: uuid.UUID | None
    allow_download: bool = False

    @property
    def changed(self) -> bool:
        return self.outcome != OUTCOME_KEPT


async def enable_public_link(
    db: AsyncSession,
    project_id: uuid.UUID,
    *,
    actor_id: uuid.UUID | None,
    regenerate: bool = False,
) -> PublicLinkChange:
    """Give the video a public link, or hand back the one it has; ``regenerate`` issues a new token.

    Upserts on the unique ``project_id``, so two managers enabling the link at
    once end with one link both are handed. Commits nothing.
    """
    token = mint_token()
    now = datetime.now(UTC)
    fields = {
        "token_hash": hash_token(token),
        "token_enc": encrypt_value(token),
        "created_by_user_id": actor_id,
        "created_at": now,
    }
    if regenerate:
        # The download option survives a new token: replacing a leaked link
        # must not silently change what its next holders are offered.
        rotated = await db.execute(
            update(VideoPublicLink)
            .where(VideoPublicLink.project_id == project_id)
            .values(**fields)
            .returning(VideoPublicLink.allow_download)
        )
        kept = rotated.scalar_one_or_none()
        if kept is not None:
            return PublicLinkChange(OUTCOME_REGENERATED, token, now, actor_id, allow_download=bool(kept))
    inserted = await db.execute(
        pg_insert(VideoPublicLink)
        .values(id=uuid.uuid4(), project_id=project_id, **fields)
        .on_conflict_do_nothing(index_elements=[VideoPublicLink.project_id])
        .returning(VideoPublicLink.id)
    )
    if inserted.scalar_one_or_none() is not None:
        # ROI of chat work (#10544): a public link is a use of the video.
        from app.services.roi_chat_records import record_signal

        await record_signal(db, kind="video", deliverable_id=project_id, signal="public_link", at=now)
        return PublicLinkChange(OUTCOME_CREATED, token, now, actor_id)
    existing = await load_public_link(db, project_id)
    if existing is None:  # pragma: no cover - disabled between the two statements
        return PublicLinkChange(OUTCOME_KEPT, None, None, None)
    return PublicLinkChange(
        OUTCOME_KEPT,
        link_token(existing),
        existing.created_at,
        existing.created_by_user_id,
        allow_download=bool(existing.allow_download),
    )


async def set_download(db: AsyncSession, project_id: uuid.UUID, allowed: bool) -> bool | None:
    """Let the link's visitors download the video, or stop them.

    ``True`` when the option changed, ``False`` when it already was *allowed*,
    ``None`` when the video has no public link. Commits nothing.
    """
    link = await load_public_link(db, project_id)
    if link is None:
        return None
    if bool(link.allow_download) == allowed:
        return False
    link.allow_download = allowed
    await db.flush()
    return True


async def disable_public_link(db: AsyncSession, project_id: uuid.UUID) -> bool:
    """Delete the video's public link: its URL stops answering at once. ``False`` when it had none."""
    removed = await db.execute(
        delete(VideoPublicLink).where(VideoPublicLink.project_id == project_id).returning(VideoPublicLink.id)
    )
    return removed.scalar_one_or_none() is not None


def audit_details(outcome: str, *, via_agent: bool = False) -> dict[str, Any]:
    """The ``details`` of the one audit row a change writes."""
    details: dict[str, Any] = {"public_link": outcome, "sharing_changed": True}
    if via_agent:
        details["via"] = "agent"
    return details


def audit_summary(outcome: str, title: str) -> str:
    verb = {
        OUTCOME_CREATED: "Turned on the public link of",
        OUTCOME_REGENERATED: "Regenerated the public link of",
        OUTCOME_DISABLED: "Turned off the public link of",
        OUTCOME_DOWNLOAD_ON: "Allowed the download from the public link of",
        OUTCOME_DOWNLOAD_OFF: "Stopped the download from the public link of",
    }[outcome]
    return f"{verb} video '{title}'"


# ── The agent's words ─────────────────────────────────────────────────────────

_AUDIENCE_NOTE = (
    "Anyone with this link can watch the video without signing in - no account, no grant. "
    "It plays the video's current render only: never its scenes, recordings or editor, and a new "
    "render replaces what it plays. Give the user the link exactly as written; it works until it is "
    "turned off (action 'disable') or replaced (action 'regenerate')."
)


def _download_sentence(allow_download: bool, *, changed: bool) -> str:
    if allow_download:
        lead = "Visitors can now also download it" if changed else "Visitors can also download it"
        return f"{lead} (the MP4 and its captions)."
    lead = "Visitors can no longer download it" if changed else "Visitors cannot download it"
    return f"{lead}: download=true lets them download the MP4 and its captions."


def format_public_link_result(
    title: str,
    outcome: str,
    token: str | None,
    *,
    rendered: bool,
    allow_download: bool = False,
    download_changed: bool = False,
) -> str:
    """What one ``set_video_public_link`` call did, worded for the user."""
    if outcome == OUTCOME_DISABLED:
        return (
            f"Public link of **{title}** turned off: the link no longer plays it, for anyone. "
            "The people it is shared with on the platform keep their access."
        )
    if token is None:
        return (
            f"**{title}** has a public link, but its address can no longer be read (the platform's "
            "encryption key changed). Call again with action 'regenerate' to issue a new link."
        )
    url = public_link_url(token)
    head = {
        OUTCOME_CREATED: f"Public link turned on for **{title}**: {url}",
        OUTCOME_REGENERATED: (
            f"New public link for **{title}**: {url}\n"
            "The previous link no longer works: whoever opens it is told the video is not available."
        ),
        OUTCOME_KEPT: f"**{title}** already has a public link, unchanged: {url}",
    }[outcome]
    lines = [head, _AUDIENCE_NOTE, _download_sentence(allow_download, changed=download_changed)]
    if not rendered:
        lines.append(
            "The video has not been rendered yet: the link says so until its first render is ready (render_video)."
        )
    return "\n".join(lines)


def public_link_status_line(*, has_link: bool, allow_download: bool = False) -> str:
    """The ``Public link:`` line of ``get_video``; empty for a video with none."""
    if not has_link:
        return ""
    if not public_links_enabled():
        return (
            "Public link: kept but not working - public video links are turned off on this platform by "
            "an administrator, so it plays nothing until they turn them back on."
        )
    download = " and download it" if allow_download else " (no download)"
    return (
        f"Public link: on - anyone with the link can watch the current render without signing in{download}. "
        "set_video_public_link returns its address (action 'enable') or turns it off ('disable')."
    )
