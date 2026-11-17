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

"""The revision history of a video's spec (``VideoProjectRevision``).

Every save that makes a revision (``projects.save_spec``) records the authored
spec it stored, who saved it and the field paths it changed per scene, so an
agent can read a video, or one scene of it, as it was at an earlier revision
(``get_video(revision=N)``), list what each revision changed
(``get_video(fields=['revisions'])``) and copy one scene back
(``edit_video`` op ``restore_scene``) instead of rebuilding it from notes.
The newest :data:`KEPT_REVISIONS` per video are kept, and the revision of every
ready whole render; an older one answers "not found" with the range still held.
"""

from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.video_studio import VideoProjectRevision

#: Revisions kept per video, the newest first; an older one is deleted by the save that makes a new one.
KEPT_REVISIONS = 100
#: The key :func:`changed_paths` files the top-level fields under (every key but ``scenes``).
VIDEO_KEY = "video"
#: Field paths listed per scene and revision; the rest is counted.
_PATHS_SHOWN = 12


def _paths(before: object, after: object, prefix: str) -> list[str]:
    """The dotted paths under *prefix* whose value differs between *before* and *after*."""
    if isinstance(before, dict) and isinstance(after, dict):
        found: list[str] = []
        for key in sorted(set(before) | set(after), key=str):
            if before.get(key) != after.get(key):
                found += _paths(before.get(key), after.get(key), f"{prefix}.{key}" if prefix else str(key))
        return found
    if isinstance(before, list) and isinstance(after, list) and len(before) == len(after):
        found = []
        for index, (old, new) in enumerate(zip(before, after, strict=True)):
            if old != new:
                found += _paths(old, new, f"{prefix}.{index}" if prefix else str(index))
        return found
    return [prefix] if before != after else []


def changed_paths(before: dict | None, after: dict) -> dict[str, list[str]]:
    """``scene id (or "video") -> [field path]`` that differ from *before* to *after*.

    A scene is matched by its id; one added or removed is listed as ``["added"]``
    or ``["removed"]``, and a scene order change is filed under ``video`` as
    ``scenes.order``.
    """
    before = before or {}
    changed: dict[str, list[str]] = {}
    top = _paths(
        {k: v for k, v in before.items() if k != "scenes"}, {k: v for k, v in after.items() if k != "scenes"}, ""
    )

    def by_id(spec: dict) -> dict[str, dict]:
        return {
            str(s.get("id")): s for s in spec.get("scenes") or [] if isinstance(s, dict) and s.get("id") is not None
        }

    old, new = by_id(before), by_id(after)
    if [k for k in old if k in new] != [k for k in new if k in old]:
        top.append("scenes.order")
    if top:
        changed[VIDEO_KEY] = top
    for scene_id in new:
        if scene_id not in old:
            changed[scene_id] = ["added"]
        elif paths := _paths(old[scene_id], new[scene_id], ""):
            changed[scene_id] = paths
    for scene_id in old:
        if scene_id not in new:
            changed[scene_id] = ["removed"]
    return changed


async def record_revision(
    db: AsyncSession,
    *,
    project_id: uuid.UUID,
    revision: int,
    spec: dict,
    previous: dict | None,
    author_label: str | None,
    via: str,
    summary: list[str] | None = None,
) -> None:
    """Record the spec *revision* stored, and drop the revisions past :data:`KEPT_REVISIONS` - except the revision of
    a ready whole render, which a re-judge reads its spec from (``render_rejudge``, issue #10899)."""
    changed = changed_paths(previous, spec) if previous is not None else {}
    db.add(
        VideoProjectRevision(
            project_id=project_id,
            revision=revision,
            spec=copy.deepcopy(spec),
            summary=list(summary or []),
            changed=changed,
            author_label=author_label,
            via=via,
        )
    )
    await db.flush()
    floor = revision - KEPT_REVISIONS
    if floor > 0:
        from app.models.video_studio import JOB_READY, VideoRender

        rendered = select(VideoRender.revision).where(
            VideoRender.project_id == project_id, VideoRender.status == JOB_READY, VideoRender.scene_id.is_(None)
        )
        await db.execute(
            delete(VideoProjectRevision).where(
                VideoProjectRevision.project_id == project_id,
                VideoProjectRevision.revision <= floor,
                VideoProjectRevision.revision.not_in(rendered),
            )
        )


class RevisionNotFoundError(LookupError):
    """No stored spec at the asked revision; the message names the range held."""


async def held_range(db: AsyncSession, project_id: uuid.UUID) -> tuple[int, int] | None:
    """The lowest and highest revision recorded for the video, or ``None``."""
    low, high = (
        await db.execute(
            select(func.min(VideoProjectRevision.revision), func.max(VideoProjectRevision.revision)).where(
                VideoProjectRevision.project_id == project_id
            )
        )
    ).one()
    return (int(low), int(high)) if low is not None else None


async def spec_at(db: AsyncSession, project_id: uuid.UUID, revision: int, *, current: int) -> dict:
    """The authored spec of the video at *revision*, or :class:`RevisionNotFoundError` naming the valid range."""
    row = (
        await db.execute(
            select(VideoProjectRevision.spec).where(
                VideoProjectRevision.project_id == project_id, VideoProjectRevision.revision == revision
            )
        )
    ).scalar_one_or_none()
    if row is not None:
        return copy.deepcopy(row)
    held = await held_range(db, project_id)
    where = (
        f"revisions {held[0]} to {held[1]} are held" if held else "no earlier revision is held (saved before history)"
    )
    raise RevisionNotFoundError(f"Revision {revision} not found: {where}; the current revision is {current}.")


@dataclass
class RevisionLine:
    revision: int
    at: str
    author: str
    via: str
    summary: list[str]
    changed: dict[str, list[str]]

    def text(self) -> str:
        parts = []
        for key, paths in self.changed.items():
            shown = ", ".join(paths[:_PATHS_SHOWN]) + (
                f" and {len(paths) - _PATHS_SHOWN} more" if len(paths) > _PATHS_SHOWN else ""
            )
            parts.append(f"{'video' if key == VIDEO_KEY else 'scene ' + key}: {shown}")
        said = "; ".join(self.summary) if self.summary else ""
        changed = " | ".join(parts) if parts else ("first revision held" if not self.summary else "")
        return (
            f"- r{self.revision} {self.at} by {self.author} ({self.via})"
            + (f": {said}" if said else "")
            + (f" [{changed}]" if changed else "")
        )


async def list_revisions(db: AsyncSession, project_id: uuid.UUID, *, limit: int = 30) -> list[RevisionLine]:
    """The newest *limit* revisions of the video, newest first."""
    rows = (
        (
            await db.execute(
                select(VideoProjectRevision)
                .where(VideoProjectRevision.project_id == project_id)
                .order_by(VideoProjectRevision.revision.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return [
        RevisionLine(
            revision=row.revision,
            at=row.created_at.strftime("%Y-%m-%d %H:%M UTC") if row.created_at else "?",
            author=row.author_label or "unknown",
            via=row.via,
            summary=list(row.summary or []),
            changed=dict(row.changed or {}),
        )
        for row in rows
    ]


def restore_scene(current: dict, earlier: dict, scene_ref: object) -> tuple[dict, str]:
    """*current* with scene *scene_ref* (an id) copied back from *earlier*, and its change line.

    Every other scene and top-level field stays as it is. A scene the current
    spec no longer has is put back at its earlier index (bounded); a scene the
    earlier spec did not have is a :class:`LookupError`.
    """
    ref = str(scene_ref)
    old = [s for s in earlier.get("scenes") or [] if isinstance(s, dict)]
    found = next(((i, s) for i, s in enumerate(old) if str(s.get("id")) == ref), None)
    if found is None:
        raise LookupError(f"no scene {ref!r} at that revision (give the scene id)")
    index, scene = found
    result = copy.deepcopy(current)
    scenes = result.setdefault("scenes", [])
    for position, now in enumerate(scenes):
        if isinstance(now, dict) and str(now.get("id")) == ref:
            scenes[position] = copy.deepcopy(scene)
            return result, f"scene {ref}: restored"
    scenes.insert(min(index, len(scenes)), copy.deepcopy(scene))
    return result, f"scene {ref}: restored at {min(index, len(scenes) - 1)}"
