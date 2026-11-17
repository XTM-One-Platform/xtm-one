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

"""Deleting a video for good: the Studio's Delete and the agent's ``delete_video``.

One path for both surfaces, so what goes with a video is the same whichever
deletes it:

* the video row, and with it (database cascades) its public link, its grants
  and its revisions;
* every render and scene preview: a render still queued or running is
  cancelled first, then each render's files (MP4, poster, captions, frames,
  findings' frames and insert crops) are deleted once the deletion committed,
  with the figures drawn for its renders and previews;
* its recordings and AI shots STAY in the library: they are the user's media,
  maybe played by another video. Their retention is stamped from now
  (:func:`retention.touch` with the video's last spec), so media no other
  video uses is purged after the retention days, the rule the storage purge
  reads.

:func:`delete_video` does the database part without committing (the caller
commits, then calls :func:`Deleted.finish`), so a refusal or a failure before
the commit leaves the video and every file in place.

The agent's bar is higher than the Studio's (:func:`agent_may_delete`): the
owner of a personal video, or an administrator on a company-managed one; a
``config`` grantee may edit a video but never makes it disappear for its owner,
the rule of ``delete_presentation``. :func:`audience` names who else opens the
video (people, groups, the whole company, a public link), which the tool asks
to confirm before deleting.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User
from app.models.video_studio import VideoProject, VideoRender


@dataclass
class Deleted:
    """What a deletion took: the renders (previews included) and the files to free once committed."""

    project_id: uuid.UUID
    title: str
    renders: int = 0
    previews: int = 0
    cancelled: int = 0
    files: list[uuid.UUID] = field(default_factory=list)

    async def finish(self) -> None:
        """After the commit: the renders' files and the drawn figures go (best effort, never raises past a file)."""
        from app.services.video_studio import assets, figure_cache

        await assets.delete_files(self.files)
        await figure_cache.evict(self.project_id)


def agent_may_delete(project: VideoProject, user: User) -> bool:
    """The owner of a personal video, or an administrator on a company-managed one."""
    if project.is_company_managed:
        return user.role == "admin"
    return project.user_id is not None and project.user_id == user.id


async def audience(db: AsyncSession, project: VideoProject) -> list[str]:
    """Who else opens *project*, in words: empty when only its owner does."""
    from app.services.video_studio import public_links

    who: list[str] = []
    if project.is_company_managed:
        who.append("everyone in the company (company-managed)")
    people = len(project.shared_user_ids or [])
    groups = len(project.shared_group_ids or [])
    if people:
        who.append(f"{people} person(s) it is shared with")
    if groups:
        who.append(f"{groups} group(s) it is shared with")
    if await public_links.load_public_link(db, project.id) is not None:
        who.append("anyone with its public link")
    return who


async def delete_video(db: AsyncSession, project: VideoProject, user: User, *, via_agent: bool = False) -> Deleted:
    """Delete *project* with its renders, previews and their files, audited; not committed (see the module)."""
    from app.services.audit import log_audit
    from app.services.video_studio import jobs, render_retention, retention

    renders = (await db.execute(select(VideoRender).where(VideoRender.project_id == project.id))).scalars().all()
    deleted = Deleted(project_id=project.id, title=project.title or "")
    for render in renders:
        if render.scene_id:
            deleted.previews += 1
        else:
            deleted.renders += 1
        deleted.files.extend(render_retention.render_files(render))
        if await jobs.cancel(db, "render", render):
            deleted.cancelled += 1
    summary = f"Deleted video {project.title}" + (" through an agent" if via_agent else "")
    await log_audit(
        db,
        user=user,
        action="delete",
        entity_type="video_project",
        entity_id=str(project.id),
        entity_name=project.title,
        summary=summary,
        details={
            "renders": deleted.renders,
            "previews": deleted.previews,
            "cancelled": deleted.cancelled,
            "files": len(deleted.files),
            **({"via_agent": True} if via_agent else {}),
        },
    )
    await retention.touch(db, project.spec or {})
    # ROI of chat work (#10544, S2b): a video deleted before any use counts nothing.
    from app.services.roi_chat_records import record_deleted

    await record_deleted(db, kind="video", deliverable_id=project.id)
    await db.delete(project)
    return deleted
