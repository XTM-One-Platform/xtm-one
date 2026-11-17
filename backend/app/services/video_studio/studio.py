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

"""Video Studio editor - a chat bound to the video it is editing.

The twin of the Presentation Studio (``services/presentation_studio.py``):
the editor docks a conversation next to a video's live preview, and the
binding ``Conversation.video_project_id`` is what makes it a studio. Per turn
(:func:`load_video_studio_context`, against the CALLING user's visibility) the
chat layer derives the context the agent needs to work on the video without
the user saying so - which video, which revision, its scenes, the scene on
screen, what the user may do to it (:meth:`VideoStudioContext.prompt`) - and
guarantees the video tools (:attr:`VideoStudioContext.tool_ids`). A video since
unshared or deleted drops out: the conversation goes on as an ordinary chat.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User
from app.models.video_studio import VideoProject
from app.services.video_studio.spec import spec_summary

#: Built-in tool ids every video studio turn carries, on top of the agent's own:
#: the video tools, and the template lookup that reads a brand's assets.
STUDIO_TOOL_IDS: tuple[str, ...] = ("video_studio", "template_search")

_ACCESS_NOTES = {
    True: "you may save edits",
    False: (
        "READ-ONLY - edit_video will refuse to save it; describe the changes you would make, "
        "or offer a copy first (create_video with its spec)"
    ),
}


@dataclass(frozen=True, slots=True)
class VideoStudioContext:
    """The video a conversation is bound to, as the calling user sees it."""

    project: VideoProject
    can_edit: bool
    #: The id of the scene on the user's screen, when the client sent one that exists.
    focused_scene: str | None = None
    tool_ids: tuple[str, ...] = STUDIO_TOOL_IDS

    def prompt(self) -> str:
        return format_video_studio_prompt(self)


async def load_video_studio_context(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    video_project_id: uuid.UUID | None,
    focused_scene: object = None,
) -> VideoStudioContext | None:
    """Resolve a conversation's video binding for one turn, or ``None``.

    ``None`` both when there is no binding and when the video is not (or no
    longer) readable by *user_id* - the agent must not learn that a video
    exists from a binding the user can no longer follow - and while the
    ``VIDEO_STUDIO`` feature flag is off.
    """
    from app.core.feature_flags import is_feature_enabled

    if video_project_id is None or not is_feature_enabled("VIDEO_STUDIO"):
        return None
    from app.services.video_studio.projects import can_manage_project, load_visible_project

    user = await db.get(User, user_id)
    if user is None:
        return None
    project = await load_visible_project(db, video_project_id, user)
    if project is None:
        return None
    scene_ids = {scene.get("id") for scene in (project.spec or {}).get("scenes") or [] if isinstance(scene, dict)}
    focused = focused_scene if isinstance(focused_scene, str) and focused_scene in scene_ids else None
    return VideoStudioContext(
        project=project, can_edit=await can_manage_project(db, project, user), focused_scene=focused
    )


def format_video_studio_prompt(ctx: VideoStudioContext) -> str:
    """The system-prompt block for a video studio turn: identity, access, the scene in view, the outline.

    Routing only - what each video tool guarantees is the tool's own
    description (invariant 16).
    """
    project = ctx.project
    spec = project.spec or {}
    scenes = [scene for scene in spec.get("scenes") or [] if isinstance(scene, dict)]
    seconds = (project.estimated_duration_ms or 0) / 1000
    style = "free style" if spec.get("freestyle") else "its template"
    lines = [
        "\n[Video Studio]",
        (
            f'The user has the video "{project.title}" open next to this chat and is editing it with you. '
            f"video_id: {project.id} - {len(scenes)} scenes, about {seconds:.0f} s, revision {project.revision}, "
            f"{(spec.get('format') or {}).get('aspect_ratio') or '16:9'}, {style}. Their preview refreshes the "
            "moment you save."
        ),
        f"- Access: {_ACCESS_NOTES[ctx.can_edit]}.",
        (
            f"- Every change goes to THIS video: edit_video with video_id={project.id} (operations for a "
            "scene, spec to rewrite it). Never create a new video unless the user asks for a copy."
        ),
    ]
    if ctx.focused_scene is not None:
        scene = next((s for s in scenes if s.get("id") == ctx.focused_scene), {})
        title = str(scene.get("title") or "").strip()
        label = f"{ctx.focused_scene} ({scene.get('type')}" + (f', "{title}"' if title else "") + ")"
        lines.append(
            f'- The user is looking at scene {label}: "this scene", "here" or "it" means that one '
            "unless they say otherwise."
        )
    lines.append("Current scenes (the indexes operations use):\n" + (spec_summary(spec) if scenes else "  (empty)"))
    return "\n".join(lines)
