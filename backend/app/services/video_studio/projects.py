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

"""Video projects: the one write path of an edit, its references and its readers.

Every write of a project's spec - the agent tools and the REST API alike -
goes through :func:`save_spec`, which validates the spec as its author wrote
it (``spec.authored_spec``), checks every id it references against what the
writer may read (:func:`verify_references`: their own recordings and clips,
files they can open, decks they can see), resolves the video template it
wears (``templates.resolve_for_spec``: the one it names, the one the project
wears, or the platform default; none for ``freestyle``), applies it
(``templates.apply_template``), refuses what only the whole render would find
otherwise (a sentence two narrations say, :func:`shared_sentence_problems`;
a recording too soft for its quad at the render the video is made for,
:func:`soft_recording_problems`), bumps the revision and recomputes the planned
duration of the EFFECTIVE spec. A save whose spec is byte-identical to the
stored one, on the same template, writes nothing (``SavedSpec.unchanged``):
no revision, no preview out of date. The project stores the authored spec and the
template id: readers and renders apply the template again
(:func:`effective_spec`), so a brand update reaches the next render.
:func:`apply_operations` is the surgical edit: one scene changed, inserted,
moved or removed, the rest untouched - on the authored spec, whose scene
indexes are the ones an author reads.

Visibility is the three-tier set (a project is shared like a deck); a
recording or a clip is its creator's, and a project shares what it shows: a
person who can open the project can play the recordings and clips it uses
(``can_read_project_asset``, one indexed lookup over every video they can
open, ``video_projects.asset_refs``).
"""

from __future__ import annotations

import copy
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.utils import format_exception_message, safe_uuid
from app.models.user import User
from app.models.video_studio import (
    JOB_PENDING,
    JOB_READY,
    JOB_RUNNING,
    VideoClip,
    VideoMusic,
    VideoProject,
    VideoRender,
    VideoTake,
)
from app.services.video_studio import card_fit, recording_fit, render_retention, retention, templates, timeline
from app.services.video_studio import music as music_tracks
from app.services.video_studio.spec import (
    MAX_SCENES,
    ONE_OF_FIELDS,
    Music,
    SpecProblem,
    VideoSpecError,
    authored_spec,
    parse_video_spec,
    referenced_ids,
    unclosed_accents,
)

logger = logging.getLogger(__name__)

OPERATIONS: tuple[str, ...] = (
    "update",
    "update_scene",
    "insert_scene",
    "remove_scene",
    "move_scene",
    "replace_scene",
    "restore_scene",
)


def restored_revisions(operations: object) -> set[int]:
    """The revisions the ``restore_scene`` operations of *operations* read, to load before :func:`apply_operations`."""
    if not isinstance(operations, list):
        return set()
    return {
        op["revision"]
        for op in operations
        if isinstance(op, dict) and op.get("op") == "restore_scene" and isinstance(op.get("revision"), int)
    }


def _scene_index(spec: dict, scene_ref: object) -> int:
    scenes = spec.get("scenes") or []
    if isinstance(scene_ref, int) or (isinstance(scene_ref, str) and scene_ref.isdigit()):
        index = int(scene_ref)
        if 0 <= index < len(scenes):
            return index
    for index, scene in enumerate(scenes):
        if scene.get("id") == scene_ref:
            return index
    raise VideoSpecError([SpecProblem("scene", f"no scene {scene_ref!r} (give its id or its 0-based index)")])


def _merge(target: dict, changes: dict) -> dict:
    """*target* with *changes* merged in: ``None`` removes a key, a field of a one-of group replaces its siblings."""
    merged = copy.deepcopy(target)
    for group in ONE_OF_FIELDS:
        chosen = {key for key in group if changes.get(key) is not None}
        if chosen:
            for sibling in group - chosen:
                merged.pop(sibling, None)
    for key, value in changes.items():
        if value is None:
            merged.pop(key, None)
        elif isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def apply_operations(
    spec: dict, operations: list[dict], earlier: dict[int, dict] | None = None
) -> tuple[dict, list[str]]:
    """The spec with *operations* applied in order, and one line per operation.

    *earlier* holds the specs ``restore_scene`` reads, by revision (:func:`restored_revisions`).
    """
    if not isinstance(operations, list) or not operations:
        raise VideoSpecError([SpecProblem("operations", "a non-empty list of operations")])
    if len(operations) > 40:
        raise VideoSpecError([SpecProblem("operations", "at most 40 operations per edit")])
    result = copy.deepcopy(spec)
    lines: list[str] = []
    for position, op in enumerate(operations):
        if not isinstance(op, dict) or op.get("op") not in OPERATIONS:
            raise VideoSpecError([SpecProblem(f"operations.{position}", f"op is one of {', '.join(OPERATIONS)}")])
        kind = op["op"]
        scenes = result.setdefault("scenes", [])
        if kind == "update":
            changes = op.get("fields")
            if not isinstance(changes, dict) or "scenes" in changes:
                raise VideoSpecError([SpecProblem(f"operations.{position}", "update takes fields (not scenes)")])
            result = _merge(result, changes)
            lines.append(f"updated {', '.join(sorted(changes))}")
        elif kind == "update_scene":
            index = _scene_index(result, op.get("scene"))
            changes = op.get("fields")
            if not isinstance(changes, dict) or "type" in changes:
                raise VideoSpecError(
                    [
                        SpecProblem(
                            f"operations.{position}", "update_scene takes fields (use replace_scene to change the type)"
                        )
                    ]
                )
            scenes[index] = _merge(scenes[index], changes)
            lines.append(f"scene {scenes[index].get('id')}: updated {', '.join(sorted(changes))}")
        elif kind == "replace_scene":
            index = _scene_index(result, op.get("scene"))
            scene = op.get("value")
            if not isinstance(scene, dict):
                raise VideoSpecError(
                    [SpecProblem(f"operations.{position}", "replace_scene takes the new scene as value")]
                )
            scene = dict(scene)
            scene.setdefault("id", scenes[index].get("id"))
            scenes[index] = scene
            lines.append(f"scene {scene.get('id')}: replaced")
        elif kind == "insert_scene":
            scene = op.get("value")
            if not isinstance(scene, dict):
                raise VideoSpecError([SpecProblem(f"operations.{position}", "insert_scene takes the scene as value")])
            if len(scenes) >= MAX_SCENES:
                raise VideoSpecError(
                    [SpecProblem(f"operations.{position}", f"a video has at most {MAX_SCENES} scenes")]
                )
            at = op.get("index", len(scenes))
            at = max(0, min(int(at), len(scenes))) if isinstance(at, int) else len(scenes)
            scenes.insert(at, dict(scene))
            lines.append(f"inserted a {scene.get('type')} scene at {at}")
        elif kind == "remove_scene":
            index = _scene_index(result, op.get("scene"))
            removed = scenes.pop(index)
            lines.append(f"removed scene {removed.get('id')}")
        elif kind == "move_scene":
            index = _scene_index(result, op.get("scene"))
            to = op.get("index")
            if not isinstance(to, int):
                raise VideoSpecError([SpecProblem(f"operations.{position}", "move_scene takes the new 0-based index")])
            scene = scenes.pop(index)
            to = max(0, min(to, len(scenes)))
            scenes.insert(to, scene)
            lines.append(f"moved scene {scene.get('id')} to {to}")
        elif kind == "restore_scene":
            from app.services.video_studio.revisions import restore_scene

            revision = op.get("revision")
            source = (earlier or {}).get(revision) if isinstance(revision, int) else None
            if source is None:
                raise VideoSpecError(
                    [SpecProblem(f"operations.{position}", "restore_scene takes scene (id) and a held revision")]
                )
            try:
                result, line = restore_scene(result, source, op.get("scene"))
            except LookupError as exc:
                raise VideoSpecError([SpecProblem(f"operations.{position}", str(exc))]) from exc
            lines.append(f"{line} from revision {revision}")
    return result, lines


def _held_ids(held: dict | None) -> dict[str, set[uuid.UUID]]:
    """The ids *held* (a stored spec) names, by kind, as UUIDs."""
    refs = referenced_ids(held or {})
    return {kind: {uid for uid in (safe_uuid(str(ref)) for ref in ids) if uid is not None} for kind, ids in refs.items()}


async def verify_references(db: AsyncSession, spec: dict, user: User, *, held: dict | None = None) -> list[SpecProblem]:
    """Every id the spec names that *user* may not use, as problems (empty = all good).

    A recording or an AI shot that failed is still the writer's to use: the
    video saves and previews with it (the preview shows the failure where it
    plays, and the shot can be generated again in place); only a render
    refuses what is not ready.

    *held* is the spec the video stores: a medium it already names
    that was deleted since is no problem - the video keeps opening, previewing
    and saving, the scene that plays it shows it missing until it is
    replaced, and only a render refuses it. A new reference to an id that
    names nothing is refused as before.
    """
    from app.api.v1.helpers import get_user_group_ids, user_visible_filter
    from app.models.file_attachment import FileAttachment
    from app.models.presentation import Presentation
    from app.services.file_access import can_read_file

    problems: list[SpecProblem] = []
    refs = referenced_ids(spec)
    kept = _held_ids(held)
    is_admin = user.role == "admin"
    for take_id in sorted(refs["take"]):
        take = await db.get(VideoTake, uuid.UUID(take_id))
        if take is None and uuid.UUID(take_id) in kept["take"]:
            continue
        if take is None or (
            take.user_id != user.id and not is_admin and not await _take_in_visible_project(db, take.id, user)
        ):
            problems.append(SpecProblem("take_id", f"no recording {take_id} you can use"))
    for clip_id in sorted(refs["clip"]):
        clip = await db.get(VideoClip, uuid.UUID(clip_id))
        if clip is None and uuid.UUID(clip_id) in kept["clip"]:
            continue
        if clip is None or (clip.user_id != user.id and not is_admin):
            problems.append(SpecProblem("clip_id", f"no clip {clip_id} you can use"))
    for music_id in sorted(refs["music"]):
        track = await db.get(VideoMusic, uuid.UUID(music_id))
        if track is None and uuid.UUID(music_id) in kept["music"]:
            continue
        if track is None or (
            track.user_id != user.id
            and not is_admin
            and not await _asset_in_visible_project(db, "music", str(track.id), user)
        ):
            problems.append(SpecProblem("music.music_id", f"no AI track {music_id} you can use"))
    unreadable: set[str] = set()
    for file_id in sorted(refs["file"]):
        row = await db.get(FileAttachment, uuid.UUID(file_id))
        if row is None and uuid.UUID(file_id) in kept["file"]:
            unreadable.add(file_id)
            continue
        if row is None or not await can_read_file(row, user, db):
            unreadable.add(file_id)
            take = await db.get(VideoTake, uuid.UUID(file_id)) if row is None else None
            recording = take is not None and await can_read_project_asset(db, "take", take, user)
            problems.append(SpecProblem("file_id", unknown_file_words(file_id, recording=recording)))
    groups = await get_user_group_ids(db, user.id)
    for deck_id in sorted(refs["presentation"]):
        found = await db.execute(
            select(Presentation.id).where(
                Presentation.id == uuid.UUID(deck_id),
                user_visible_filter(Presentation, user.id, groups, is_admin=is_admin),
            )
        )
        if found.scalar_one_or_none() is not None:
            continue
        if uuid.UUID(deck_id) in kept["presentation"] and await db.get(Presentation, uuid.UUID(deck_id)) is None:
            continue
        problems.append(SpecProblem("presentation_id", f"no presentation {deck_id} you can open"))
    problems.extend(await _figure_address_problems(db, spec, user, groups, is_admin=is_admin))
    problems.extend(await _lottie_problems(db, spec, unreadable))
    # The template is checked where it is resolved (``templates.resolve_for_spec``).
    return problems


def unknown_file_words(file_id: str, *, recording: bool = False) -> str:
    """Why a ``file_id`` is refused: no file the person can read - one that is a recording's id says so, with
    the form a frame of it takes."""
    if recording:
        return (
            f"{file_id} is a recording, not a file: a picture of a video takes a frame of it as "
            f'{{"take_id": "{file_id}", "at_step": N}} (or at_seconds); a recording scene plays it as {{"type": '
            f'"recording", "take_id": "{file_id}"}}'
        )
    return f"no file {file_id} you can read"


async def _lottie_problems(db: AsyncSession, spec: dict, unreadable: set[str]) -> list[SpecProblem]:
    """A Lottie element whose file a motion scene may not play: refused at save, the reason said.

    Read only when the file itself passed (*unreadable*: a file the writer
    cannot read, already said, or one gone since the video named it); a file
    loading anything from outside itself is refused.
    """
    from app.services.video_studio import motion_design

    found: list[SpecProblem] = []
    for index, scene in enumerate(spec.get("scenes") or []):
        if not isinstance(scene, dict) or scene.get("type") != "motion":
            continue
        for number, element in enumerate(scene.get("elements") or []):
            if not isinstance(element, dict) or element.get("kind") != "lottie" or not element.get("file_id"):
                continue
            file_id = str(element["file_id"])
            if file_id in unreadable:
                continue
            check = await motion_design.read_lottie(db, file_id)
            if check is not None and check.problem:
                found.append(SpecProblem(f"scenes.{index}.elements.{number}.file_id", check.problem))
    return found


async def _figure_address_problems(
    db: AsyncSession, spec: dict, user: User, groups: list, *, is_admin: bool
) -> list[SpecProblem]:
    """A figure scene's deck address that names no block - no such slide, no such block - as problems."""
    from types import SimpleNamespace

    from app.api.v1.helpers import user_visible_filter
    from app.models.presentation import Presentation
    from app.services.video_studio import figures

    problems: list[SpecProblem] = []
    for index, scene in enumerate(spec.get("scenes") or []):
        if scene.get("type") != "figure" or not scene.get("presentation_id"):
            continue
        deck = (
            await db.execute(
                select(Presentation.spec).where(
                    Presentation.id == uuid.UUID(scene["presentation_id"]),
                    user_visible_filter(Presentation, user.id, groups, is_admin=is_admin),
                )
            )
        ).scalar_one_or_none()
        if deck is None:
            continue  # already said: no presentation you can open
        address = SimpleNamespace(
            id=scene.get("id") or f"figure-{index + 1}",
            slide=scene.get("slide") or (None if scene.get("slide_id") else 1),
            slide_id=scene.get("slide_id"),
            block=int(scene.get("block") or 0),
        )
        try:
            figures.source_from_deck(address, dict(deck or {}), uuid.UUID(scene["presentation_id"]))
        except figures.FigureError as exc:
            problems.append(SpecProblem(f"scenes.{index}", str(exc)))
    return problems


async def _take_in_visible_project(db: AsyncSession, take_id: uuid.UUID, user: User) -> bool:
    return await _asset_in_visible_project(db, "take", str(take_id), user)


async def _asset_in_visible_project(db: AsyncSession, kind: str, asset_id: str, user: User) -> bool:
    """Whether a video *user* can open names the medium, however many videos they see (``retention.uses_media``)."""
    from app.api.v1.helpers import get_user_group_ids, user_visible_filter

    keys = retention.media_keys([(kind, asset_id)])
    if not keys:
        return False
    groups = await get_user_group_ids(db, user.id)
    rows = await db.execute(
        select(VideoProject.id)
        .where(
            user_visible_filter(VideoProject, user.id, groups, is_admin=user.role == "admin"),
            retention.uses_media(keys),
        )
        .limit(1)
    )
    return rows.scalars().first() is not None


async def can_read_project_asset(
    db: AsyncSession, kind: str, asset: VideoTake | VideoClip | VideoMusic, user: User
) -> bool:
    """A recording, clip or AI track is readable by its creator, an admin, or anyone who can open a project using it."""
    if user.role == "admin" or asset.user_id == user.id:
        return True
    return await _asset_in_visible_project(db, kind, str(asset.id), user)


async def _take_media(db: AsyncSession, spec: dict) -> dict[str, timeline.TakeMedia]:
    media: dict[str, timeline.TakeMedia] = {}
    for take_id in referenced_ids(spec)["take"]:
        take = await db.get(VideoTake, uuid.UUID(take_id))
        if take is None or take.status != JOB_READY:
            continue
        media[take_id] = take_media(take)
    return media


async def _planned_clips(db: AsyncSession, spec: Any) -> dict[str, timeline.ClipMedia]:
    """The clip scenes' AI shots as the preview plans them, by scene id (an upload is probed at render)."""
    from app.services.video_studio.preview import planned_clip

    return {
        scene.id: await planned_clip(db, scene.clip_id)
        for scene in spec.scenes
        if scene.type == "clip" and scene.clip_id and scene.id
    }


async def estimate(
    db: AsyncSession, effective: dict, *, voices: dict[str, float] | None = None, beats: Any = None
) -> timeline.TimelinePlan:
    """The plan of an *effective* spec (the template applied) before its render: its recordings' and shots' lengths,
    its narrations at their spoken length where *voices* (``preview.spoken_lengths``) has them, and its cuts
    on the music's *beats* (``beats.video_grid``) when it has them."""
    spec = parse_video_spec(effective)
    return timeline.estimate_plan(
        spec, await _take_media(db, effective), await _planned_clips(db, spec), voices, beats=beats
    )


@dataclass(frozen=True)
class MeasuredPlan:
    """The plan of a video as its render will place it, from what is measured before it (:func:`measured_plan`)."""

    plan: timeline.TimelinePlan
    #: ``scene id -> seconds`` of each narration placed at the length its voice take measured (``preview.spoken_lengths``).
    spoken: dict[str, float]
    #: How many narrations the voice-over speaks: those not in :attr:`spoken` are placed at the reading pace.
    narrated: int
    #: Why a ``beat_sync`` video lands nothing on the music's beats (``beats.video_grid``); ``None`` when it does, or
    #: does not ask.
    beat_note: str | None = None

    @property
    def duration(self) -> float:
        return self.plan.duration

    def basis(self) -> str:
        """``12 of 14 narrations at their spoken length, 2 at the reading pace``: what the length is measured on."""
        if not self.narrated:
            return ""
        spoken = min(len(self.spoken), self.narrated)
        which = "every narration" if self.narrated > 1 else "its narration"
        if spoken == self.narrated:
            return f"{which} at its spoken length"
        if not spoken:
            return f"{which} at the reading pace, not spoken yet"
        return f"{spoken} of {self.narrated} narrations at their spoken length, {self.narrated - spoken} at the reading pace"


async def measured_plan(db: AsyncSession, effective: dict, user: User) -> MeasuredPlan:
    """The plan of an *effective* spec as its render will place it, from what is measured before it.

    Each narration - the template cards' included - at the length its voice take measured where one was spoken in
    the video's voice (``preview.spoken_lengths``, the cache the render reads), the others at the reading pace; a
    ``beat_sync`` video's cuts on the music's beats (``beats.video_grid``), each scene a little longer, as the render
    and the preview land them. What the save's length, the music's fit and the quality check read.
    """
    from app.services.video_studio import beats, preview

    spec = parse_video_spec(effective)
    spoken = await preview.spoken_lengths(db, spec, user) if spec.voiceover.enabled else {}
    grid, beat_note = await beats.video_grid(spec, db=db)
    plan = await estimate(db, effective, voices=spoken, beats=grid)
    narrated = sum(1 for scene in spec.scenes if scene.narration) if spec.voiceover.enabled else 0
    return MeasuredPlan(plan, spoken, narrated, beat_note)


async def preview_transitions(db: AsyncSession, effective: dict) -> dict[str, tuple[dict | None, dict | None]]:
    """``scene id -> (its transition in, its transition out)`` as the window of its preview plans them.

    A preview plans a scene with the one before and the one after it
    (``timeline.preview_window``), where each transition is decided by the two
    scenes it joins and the length of the first (``timeline.auto_transition``).
    Planned here as :func:`estimate` plans - the recordings' and shots'
    lengths, the narration paced: ``None`` where the window has no such
    transition (the video's first or last scene), or could not be planned.
    """
    spec = parse_video_spec(effective)
    takes, shots = await _take_media(db, effective), await _planned_clips(db, spec)
    found: dict[str, tuple[dict | None, dict | None]] = {}
    for index, scene in enumerate(spec.scenes):
        if not scene.id:
            continue
        first, end = timeline.preview_window(len(spec.scenes), index)
        window = spec.model_copy(update={"scenes": spec.scenes[first:end]})
        try:
            planned = timeline.estimate_plan(window, dict(takes), shots).document["scenes"]
        except ValueError:
            found[scene.id] = (None, None)
            continue
        target = index - first
        found[scene.id] = (
            planned[target]["transition_in"] if target > 0 else None,
            planned[target + 1]["transition_in"] if target + 1 < len(planned) else None,
        )
    return found


async def estimate_seconds(db: AsyncSession, effective: dict) -> float:
    """The planned length of an *effective* spec (the template applied), before its voice exists."""
    return (await estimate(db, effective)).duration


async def shot_uses(db: AsyncSession, effective: dict) -> list[templates.ShotUse]:
    """The AI shots *effective* plays - clip scenes and statement backdrops - made under a template's art direction.

    A shot made before shots had a direction, in free style or from a picture
    (whose look is the picture's) is the author's choice: not listed.
    """
    from app.services.video_studio import clips

    uses: list[templates.ShotUse] = []
    for scene in effective.get("scenes") or []:
        kind = scene.get("type")
        source = scene if kind == "clip" else (scene.get("backdrop") or {}) if kind == "statement" else {}
        clip_id = safe_uuid(str(source.get("clip_id") or ""))
        clip = await db.get(VideoClip, clip_id) if clip_id else None
        params = (clip.params or {}) if clip is not None else {}
        direction = params.get(clips.DIRECTION)
        if not isinstance(direction, dict) or clips.starts_from_picture(params):
            continue
        uses.append(
            templates.ShotUse(
                scene_id=str(scene.get("id")),
                clip_id=str(clip.id),
                directed_by=str(direction.get("template_id")),
                directed_by_name=str(direction.get("template") or "another template"),
            )
        )
    return uses


async def transitions_line(db: AsyncSession, effective: dict) -> str:
    """How the *effective* spec cuts into each scene, as planned now: what a tool result shows before a render.

    ``auto`` is resolved here as the render resolves it (the scenes' lengths
    before the voice is spoken, so a cut may still become a dissolve once a
    narration lengthens the scene before it).
    """
    spec = parse_video_spec(effective)
    plan = await estimate(db, effective)
    asked = {scene.id: (scene.transition or spec.default_transition) for scene in spec.scenes}
    parts = []
    for scene_id, kind, seconds in timeline.planned_transitions(plan):
        wanted = asked.get(scene_id)
        how = f" (auto, {wanted.pace})" if wanted is not None and wanted.type == "auto" else ""
        parts.append(f"{scene_id}: {kind}{f' {seconds:.2f} s' if kind != 'cut' else ''}{how}")
    return "Transitions into each scene, as planned now: " + ("; ".join(parts) if parts else "one scene, none") + "."


async def planned_seconds(db: AsyncSession, project: VideoProject, user: User) -> float:
    """The planned length of *project* now, as *user* sees its template: what an AI track is composed for - each
    narration at its spoken length where it was spoken, its cuts on the beats (:func:`measured_plan`).

    The length stored at its last save when the spec no longer validates with its template.
    """
    try:
        effective = (await effective_spec(db, project, user)).spec
    except VideoSpecError:
        return (project.estimated_duration_ms or 60_000) / 1000
    return (await measured_plan(db, effective, user)).duration


async def rendered_seconds(db: AsyncSession, project: VideoProject) -> float | None:
    """How long *project*'s current render lasts, when it renders the video's current revision.

    The render measured the voice the plan only estimates: a track composed
    for the plan alone ended before such a render did. ``None`` without a
    current render ready, or for one of an older revision.
    """
    current = getattr(project, "latest_render_id", None)
    render = await db.get(VideoRender, current) if current is not None else None
    if render is None or render.status != JOB_READY or render.scene_id is not None or not render.duration_ms:
        return None
    if render.revision != project.revision:
        return None
    return render.duration_ms / 1000


async def render_music_line(
    db: AsyncSession, project: VideoProject, user: User, spec: Any = None, *, music_changed: bool = False
) -> str | None:
    """Whether the video's current render was made with the music it plays now (``music.render_music_note``).

    *spec* is the effective spec, parsed, when the caller has it; *music_changed*
    says the save asking changed what music plays. ``None`` without a current
    render ready, or when neither plays music.
    """
    current = getattr(project, "latest_render_id", None)
    if current is None:
        return None
    render = await db.get(VideoRender, current)
    if render is None or render.status != JOB_READY:
        return None
    if spec is None:
        try:
            spec = parse_video_spec((await effective_spec(db, project, user)).spec)
        except VideoSpecError:
            return None
    return await music_tracks.render_music_note(db, render, spec.music, music_changed=music_changed)


def _music_source(spec: dict) -> tuple[str, str] | None:
    """What music an authored spec names (``Music.source``); ``None`` for none or one that no longer reads."""
    raw = spec.get("music")
    if not isinstance(raw, dict):
        return None
    try:
        return Music.model_validate(raw).source
    except ValidationError:
        return None


def take_media(take: VideoTake) -> timeline.TakeMedia:
    from app.services.video_studio import insert_floor

    return timeline.TakeMedia(
        take_id=str(take.id),
        duration=(take.duration_ms or 0) / 1000,
        css_width=take.viewport_width or 1600,
        css_height=take.viewport_height or 900,
        pixel_ratio=take.pixel_ratio or 1.0,
        fps=take.fps or 60,
        frame_count=max(1, round((take.duration_ms or 0) / 1000 * (take.fps or 60))),
        events=list(take.events or []),
        cursor=[tuple(c) for c in (take.cursor or [])],
        captured_frames=take.captured_frames,
        sparse=list((getattr(take, "capture", None) or {}).get("sparse") or []),
        low=list((getattr(take, "capture", None) or {}).get("low") or []),
        stepping=list((getattr(take, "capture", None) or {}).get("stepping") or []),
        off_scale=list((getattr(take, "capture", None) or {}).get("off_scale") or []),
        insert_rate=insert_floor.measured_of(getattr(take, "capture", None)),
    )


@dataclass(frozen=True)
class MediaState:
    """Where the media a spec shows stand (:func:`media_state`)."""

    #: A digest of each one's status, the take it plays and when its last attempt ended.
    key: str
    #: The AI shots generated again in place, their previous take playing meanwhile.
    regenerating: list[str]


async def media_state(db: AsyncSession, spec: dict) -> MediaState:
    """Where the recordings, AI shots and AI tracks *spec* names stand: what the quality check's picture findings read.

    A shot generated again in place changes no revision; it changes the key,
    so the editor reads the findings again, and it is listed while it
    is being made, so the editor watches for it.
    """
    import hashlib

    from app.services.video_studio import clips

    refs = referenced_ids(spec)
    parts: list[str] = []
    regenerating: list[str] = []
    for model, kind in ((VideoTake, "take"), (VideoClip, "clip"), (VideoMusic, "music")):
        for uid in sorted({uid for uid in (safe_uuid(str(ref)) for ref in refs[kind]) if uid is not None}, key=str):
            # Read already by whoever planned the spec: the session hands its own copy back.
            row = await db.get(model, uid)
            if row is None:
                parts.append(f"{kind}:{uid}:gone")
                continue
            ended = row.finished_at.isoformat() if getattr(row, "finished_at", None) else ""
            parts.append(f"{kind}:{uid}:{row.status}:{row.file_id}:{ended}")
            if kind == "clip" and row.status in (JOB_PENDING, JOB_RUNNING) and clips.keeps_take(row.params):
                regenerating.append(str(uid))
    return MediaState(hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16], regenerating)


async def silent_clip_scenes(db: AsyncSession, spec: Any) -> set[str]:
    """The clip scenes of *spec* (parsed) whose AI shot plays a take with no sound track."""
    wanted = {scene.clip_id for scene in spec.scenes if scene.type == "clip" and scene.clip_id}
    ids = [uid for uid in (safe_uuid(str(clip_id)) for clip_id in wanted) if uid is not None]
    if not ids:
        return set()
    rows = (await db.execute(select(VideoClip.id, VideoClip.file_id, VideoClip.has_audio).where(VideoClip.id.in_(ids)))).all()
    mute = {str(row.id) for row in rows if row.file_id and not row.has_audio}
    return {str(scene.id) for scene in spec.scenes if scene.type == "clip" and scene.clip_id in mute}


@dataclass
class SavedSpec:
    project: VideoProject
    #: What the author wrote (validated), as stored.
    authored: dict
    #: What renders: the template applied, every default filled.
    applied: templates.AppliedTemplate
    seconds: float
    #: Image scenes whose animation started with this save, and why others play as stills.
    animations: int = 0
    notes: list[str] = field(default_factory=list)
    #: AI tracks this save started (a ``music.prompt`` without its track yet).
    music_started: int = 0
    #: The figure scenes whose editor still is drawn once the save is committed (:func:`queue_figure_stills`).
    figure_stills: list = field(default_factory=list)
    #: How the save's plan times each figure scene (``figures.FigureTiming``): when its steps come in, said by the save.
    figure_timings: dict = field(default_factory=dict)
    #: The scenes drawn before that differ now (``render_checks.changed_scenes``): an edit marks its scene and
    #: its neighbours, never the scenes it left alone.
    changed_since_preview: list[str] = field(default_factory=list)
    #: The scenes back to a drawing older than their last (``render_checks.restored_scenes``): an edit reverted.
    restored_preview: list[str] = field(default_factory=list)
    #: The scenes whose passing preview still holds after the save (``render_checks.holding_scenes``): a render
    #: needs no preview of them.
    preview_holds: list[str] = field(default_factory=list)
    #: ``scene id -> what changed in it`` of :attr:`changed_since_preview` (``render_checks.changed_parts``).
    changed_parts: dict[str, list[str]] = field(default_factory=dict)
    #: The spec saved is byte-identical to the stored one on the same template: nothing was written.
    unchanged: bool = False
    #: The whole renders in progress this save carried to its revision, nothing they draw changed.
    carried: list = field(default_factory=list)
    #: What :attr:`seconds` is measured on (:meth:`MeasuredPlan.basis`): its narrations spoken or paced.
    seconds_basis: str = ""


def same_spec(stored: dict | None, authored: dict) -> bool:
    """Whether *authored* is *stored* byte for byte, as canonical JSON: a save of it would change nothing."""

    def canonical(value: object) -> str:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)

    return stored is not None and canonical(stored) == canonical(authored)


def figure_timings_of(plan: timeline.TimelinePlan, effective: dict) -> dict:
    """``scene id -> figures.FigureTiming`` of every figure scene *plan* places."""
    from app.services.video_studio import figures

    found = {}
    for scene in effective.get("scenes") or []:
        if scene.get("type") == "figure" and scene.get("id") and (timing := figures.figure_timing(plan, scene["id"])):
            found[scene["id"]] = timing
    return found


async def figure_stills_of(db: AsyncSession, user: User, effective: dict, template_id: object) -> list:
    """The figure scenes of a saved spec as their stills are drawn - never a reason for a save to fail. Read by the save
    and by the quality check (``quality._figure_fits``), with :func:`figure_timings_of`."""
    from app.core.utils import format_exception_message
    from app.services.presentations import resolve_template_theme
    from app.services.video_studio import figures

    spec = parse_video_spec(effective)
    if not any(scene.type == "figure" for scene in spec.scenes):
        return []
    palette: dict = {}
    try:
        if template_id:
            # The palette the preview resolves for the same reader (``preview._theme``): the same digest.
            palette = await resolve_template_theme(
                db, uuid.UUID(str(template_id)), user_id=user.id, deck_theme=spec.theme.mode, is_admin=user.role == "admin"
            )
    except Exception:  # the preview falls back to no palette as well
        palette = {}
    try:
        return list((await figures.figure_previews(db, user, spec, palette)).values())
    except Exception as exc:
        logger.info("The figure stills of a save were not planned: %s", format_exception_message(exc))
        return []


async def queue_figure_stills(saved: SavedSpec, user: User) -> list[str]:
    """Hand, once the save is committed, the stills of its figures not drawn yet to the worker (``figure_stills``).

    Answers at once: the editor's preview names each figure until
    its still is drawn, then shows it built in whole. Returns the note the
    tool answers carry, the template assets each figure draws as their twins
    for the video's scheme (``figures.twin_lines``), then when each
    figure's steps come in as the save plans them (``figures.reveal_lines``)
    and the drift each plays, judged as it is drawn in motion
    (``figures.fit_lines``).
    """
    from app.services.video_studio import figure_stills, figures

    if not saved.figure_stills:
        return []
    note = figure_stills.queued_note(await figure_stills.enqueue(saved.figure_stills, user))
    return [
        *([note] if note else []),
        *figures.twin_lines(saved.figure_stills),
        *await figures.reveal_lines(saved.figure_stills, saved.figure_timings),
        *await figures.fit_lines(saved.figure_stills, saved.figure_timings),
    ]


#: How an animated image moves when its author says nothing: the camera, never the content.
DEFAULT_ANIMATION_PROMPT = (
    "Bring this image to life: a slow, smooth cinematic camera move with subtle natural motion and light. "
    "Keep every element, text, number and logo exactly as it is in the image."
)


def _animation_shots(spec: dict | None) -> dict[tuple[str, str], str]:
    """``(scene id, file id) -> clip id`` of the animations *spec* already has."""
    found: dict[tuple[str, str], str] = {}
    for scene in (spec or {}).get("scenes") or []:
        animate = scene.get("animate") if isinstance(scene, dict) else None
        if isinstance(animate, dict) and animate.get("clip_id") and scene.get("id") and scene.get("file_id"):
            found[(scene["id"], scene["file_id"])] = animate["clip_id"]
    return found


async def start_animations(
    db: AsyncSession,
    user: User,
    authored: dict,
    effective: dict,
    *,
    previous: dict | None = None,
    conversation_id: object = None,
    template: templates.VideoTemplate | None = None,
) -> tuple[int, list[str]]:
    """Start the AI shot of every image scene asking to be animated, in place in *authored*.

    A scene saved without its shot (an editor's draft predating it) keeps the
    one *previous* - the stored spec - has for the same scene and image: a
    save never pays for the same animation twice. A scene whose shot was made
    from another image (its ``file_id`` changed) gets a new one. Returns how
    many started, and a note for each scene left a still because no video
    model can make its shot. A picture no provider can read fails the save
    (:class:`VideoSpecError` naming the scene) rather than the render.
    An animation is a moving picture: its shot is asked silent, under the art
    direction of the *template* the video wears. Its ``keep`` regions
    are kept from the still in every frame (``kept_regions``): a shot
    made with other regions is made again, the save saying so.
    """
    from app.services.video_studio import clips, kept_regions

    started, notes = 0, []
    ratio = (effective.get("format") or {}).get("aspect_ratio") or "16:9"
    kept = _animation_shots(previous)
    for index, scene in enumerate(authored.get("scenes") or []):
        animate = scene.get("animate") if scene.get("type") == "image" else None
        if not isinstance(animate, dict):
            continue
        if not animate.get("clip_id") and (inherited := kept.get((scene.get("id"), scene.get("file_id")))):
            animate["clip_id"] = inherited
        if animate.get("clip_id"):
            clip = await db.get(VideoClip, uuid.UUID(animate["clip_id"]))
            made_from = ((clip.params or {}).get("reference_file_id") if clip is not None else None) or ""
            if clip is not None and made_from == scene.get("file_id"):
                if kept_regions.same_boxes(kept_regions.boxes_of(clip.params), animate.get("keep")):
                    continue
                notes.append(f"Scene {scene.get('id')}: its kept regions changed, so a new animation was started.")
        try:
            clip, _label = await clips.start_clip(
                db,
                user,
                prompt=animate.get("prompt") or DEFAULT_ANIMATION_PROMPT,
                duration_seconds=int(min(8, max(4, round(float(scene.get("duration") or 4))))),
                aspect_ratio=ratio if ratio in ("16:9", "9:16") else "16:9",
                resolution="1080p",
                reference_file_id=scene["file_id"],
                conversation_id=safe_uuid(str(conversation_id)) if conversation_id else None,
                name=f"Animated image ({scene.get('id')})",
                sound=False,
                # The default prompt moves the camera and names no subject: nothing to read the frames for.
                content_check=bool(animate.get("prompt")),
                template_id=template.id if template is not None else None,
                freestyle=template is None,
                keep_regions=animate.get("keep") or None,
            )
        except clips.StartPictureError as exc:
            raise VideoSpecError(
                [SpecProblem(f"scenes.{index}.file_id", f"scene {scene.get('id')} cannot be animated: {exc}")]
            ) from None
        except clips.ClipError as exc:
            animate.pop("clip_id", None)
            notes.append(f"Scene {scene.get('id')} plays as a still: {exc}")
            continue
        animate["clip_id"] = str(clip.id)
        started += 1
    return started, notes


async def _fit_recordings(db: AsyncSession, user: User, effective: dict) -> list[recording_fit.Fit]:
    """The speeds the save sets on the recording scenes of *effective* their narration outruns, planned with
    each narration at its spoken length where it was spoken, as the quality check plans it."""
    from app.services.video_studio import preview

    spec = parse_video_spec(effective)
    if not any(scene.type == "recording" and scene.fit == "auto" for scene in spec.scenes):
        return []
    spoken = await preview.spoken_lengths(db, spec, user) if spec.voiceover.enabled else {}
    plan = await estimate(db, effective, voices=spoken)
    return recording_fit.fits(spec, plan.document, await _take_media(db, effective), plan.narration_spans)


def _scene_positions(authored: dict) -> dict[str, int]:
    return {scene["id"]: index for index, scene in enumerate(authored.get("scenes") or []) if isinstance(scene, dict) and scene.get("id")}


def _scenes_words(scene_ids: tuple[str, ...]) -> str:
    named = [f"`{scene_id}`" for scene_id in scene_ids]
    return named[0] if len(named) == 1 else f"{', '.join(named[:-1])} and {named[-1]}"


def shared_sentence_problems(authored: dict, effective: dict) -> list[SpecProblem]:
    """Each sentence two narrations of *effective* say (``audio_qc.shared_sentences``), refused at save with
    their scenes: each scene's preview hears its own narration alone and passes, the whole render hears the sentence
    more times than either writes it. Placed at the narration of the last of them *authored* writes (a template's card
    speaks the others)."""
    from app.services.video_studio import audio_qc, voice

    spec = parse_video_spec(effective)
    if not spec.voiceover.enabled:
        return []
    positions = _scene_positions(authored)
    problems = []
    for shared in audio_qc.shared_sentences([(line.scene_id, line.text) for line in voice.lines_of(spec)]):
        last = next((positions[scene_id] for scene_id in reversed(shared.scene_ids) if scene_id in positions), None)
        problems.append(
            SpecProblem(
                f"scenes.{last}.narration" if last is not None else "narration",
                f'scenes {_scenes_words(shared.scene_ids)} each say "{shared.sentence[:160]}": a scene preview hears '
                f"its own narration alone, the whole render hears it {len(shared.scene_ids)} times and fails its audio "
                "check (repeated_sentence) - say it in one narration, or reword the others",
            )
        )
    return problems


async def soft_recording_problems(db: AsyncSession, project: VideoProject | None, authored: dict, effective: dict) -> list[SpecProblem]:
    """Each recording a screen insert of *effective* draws too soft for the whole render the video is made for -
    its newest whole render's quality, ``spec.FINAL_QUALITY`` before its first
    (``render_checks.soft_recordings``) -, refused at save with the window and the pixel ratio to record it at: a
    preview draws it smaller, so only that render would refuse it, once every scene was previewed."""
    from app.services.video_studio import render_checks
    from app.services.video_studio.spec import FINAL_QUALITY

    quality = await render_checks.target_quality(db, project, default=FINAL_QUALITY)
    positions = _scene_positions(authored)
    problems = []
    for item in await render_checks.soft_recordings(db, parse_video_spec(effective), quality):
        soft = item.soft
        at = positions.get(soft.scene_id)
        field = f"sources.{soft.pane}" if soft.pane is not None else "source"
        path = f"scenes.{at}.overlays.{soft.index}.{field}" if at is not None else f"scenes.{soft.scene_id}"
        problems.append(SpecProblem(path, f"scene {soft.scene_id}: {item.words()}"))
    return problems


async def save_spec(
    db: AsyncSession,
    *,
    user: User,
    raw: Any,
    project: VideoProject | None = None,
    conversation_id: object = None,
    held: dict | None = None,
    via: str = "studio",
    summary: list[str] | None = None,
) -> SavedSpec:
    """Validate and store a spec (a new project when *project* is ``None``), or :class:`VideoSpecError`.

    A spec that comes out byte-identical to the stored one, on the same
    template, once its shots and its AI track are named (``start_animations``,
    ``music.start_for_spec``), is not written: the revision stays, so
    do its previews and its renders, and :attr:`SavedSpec.unchanged` says so.
    The media deleted since a video named them (``verify_references``) are
    those of *held*, the project's stored spec unless given (a duplicate's source).
    A revision made is recorded in the history (``revisions.record_revision``)
    with *via* and the *summary* lines of the edit.
    """
    authored = templates.keep_look(authored_spec(raw), project.spec if project is not None else None)
    if held is None and project is not None:
        held = dict(project.spec or {})
    # A recording's frame named as a picture is kept as one, before anything reads the pictures.
    from app.services.video_studio import take_frames

    authored, frame_notes = await take_frames.resolve(
        db, user, authored, previous=held, conversation_id=safe_uuid(str(conversation_id)) if conversation_id else None
    )
    problems = await verify_references(db, authored, user, held=held)
    if problems:
        raise VideoSpecError(problems)
    container = await templates.resolve_for_spec(
        db, user, authored, current_id=project.template_container_id if project is not None else None
    )
    template = await templates.load_video_template(db, container)
    applied = templates.apply_template(authored, template)
    effective = applied.spec
    # A sentence two narrations say passes each scene's preview and fails the whole render: refused before any.
    if shared := shared_sentence_problems(authored, effective):
        raise VideoSpecError(shared)
    # Each static screen insert on the bezel its first footage frame shows, measured before anything is drawn:
    # a quad left to the footage written, a quad near it snapped, no bezel or a quad far off refused. A tracked one's
    # quad snapped to that bezel, where its track starts, and its track followed and said. A scene the edit left
    # identical to the stored one keeps its stored quads: they were snapped when stored, so its preview stays valid.
    from app.services.video_studio import insert_bezels

    # Several inserts of a scene left to the footage are handed their screens by the quads stored before: a re-measure.
    stored = insert_bezels.stored_quads(dict(project.spec or {})) if project is not None else None
    measured = await insert_bezels.measure(db, parse_video_spec(effective), user=user, tracks=True, previous=stored)
    bezels = insert_bezels.resolve(authored, measured, held=dict(project.spec or {}) if project is not None else None)
    if bezels.problems:
        raise insert_bezels.BezelRefusedError(bezels.problems, bezels.refused)
    if bezels.changed:
        authored = bezels.authored
        applied = templates.apply_template(authored, template)
        effective = applied.spec
    # A recording too soft for its quad at the render the video is made for: recorded again once, at the size
    # named, before any preview - its quads are known now, the ones left to the footage written above.
    if soft := await soft_recording_problems(db, project, authored, effective):
        raise VideoSpecError(soft)
    recordings_fitted = await _fit_recordings(db, user, effective)
    if recordings_fitted:
        authored = recording_fit.apply(authored, recordings_fitted)
        applied = templates.apply_template(authored, template)
        effective = applied.spec
    # As the render will place it: the voice takes spoken, the template cards' included, the cuts on the beats -
    # the length the music is fitted to and checked against.
    placed = await measured_plan(db, effective, user)
    # A play-once track ending before the video: the cards the template adds at the least their words take, the outro
    # first, before the cards lengthen the video past it - kept when the video then plans shorter.
    card_notes: list[str] = []
    if (shortening := await card_fit.shortened(db, authored, applied, placed)) is not None:
        fitted = templates.apply_template(shortening.authored, template)
        refitted = await measured_plan(db, fitted.spec, user)
        if refitted.duration < placed.duration - 0.05:
            authored, applied, effective, placed = shortening.authored, fitted, fitted.spec, refitted
            card_notes.append(shortening.words(placed.duration))
    planned, estimated = placed.plan, placed.duration
    template_id = container.id if container is not None else None
    animations, notes = await start_animations(
        db,
        user,
        authored,
        effective,
        previous=dict(project.spec or {}) if project is not None else None,
        conversation_id=conversation_id,
        template=template,
    )
    notes.extend(bezels.notes)
    notes.extend(frame_notes)
    notes.extend(fit.words() for fit in recordings_fitted)
    notes.extend(card_notes)
    # A screen insert playing a take painted under the floor an insert source needs: said now, with the call that
    # records it again, before a scene preview's lint refuses it (``insert_floor.save_notes``, #10079).
    from app.services.video_studio import insert_floor

    notes.extend(await insert_floor.save_notes(db, parse_video_spec(effective), project))
    music_started, music_notes = await music_tracks.start_for_spec(
        db,
        user,
        authored,
        planned_seconds=estimated,
        previous=dict(project.spec or {}) if project is not None else None,
        conversation_id=safe_uuid(str(conversation_id)) if conversation_id else None,
    )
    notes.extend(music_notes)
    if project is not None and project.template_container_id == template_id and same_spec(project.spec, authored):
        # Nothing started above either: a shot or a track started names itself in the spec, which then differs.
        return SavedSpec(project, authored, applied, estimated, notes=notes, unchanged=True, seconds_basis=placed.basis())
    notes.extend(unclosed_accents(authored))
    if bare := templates.watermark_note(authored, effective, template):
        notes.append(bare)
    playing = effective.get("music") if isinstance(effective.get("music"), dict) else None
    if playing and playing.get("enabled", True) and not any(playing.get(k) for k in ("track", "music_id", "file_id", "prompt")):
        notes.append(
            "The music names no track, music_id, file_id or prompt: the video plays no music "
            "(video_studio_capabilities lists the built-in tracks)."
        )
    replaced = dict(project.spec or {}) if project is not None else {}
    # The template may bring the music: changing it may change what plays.
    music_changed = project is not None and (
        _music_source(replaced) != _music_source(authored) or project.template_container_id != template_id
    )
    # The whole renders in progress of the revision this save replaces, locked before the project is written:
    # those this revision draws exactly as they do are carried to it below. A queued one is compared on the spec it
    # was asked for, read now as it was.
    carrying = await renders_to_carry(db, project) if project is not None else []
    from_revision = project.revision if project is not None else 0
    before = None
    if any(render.started_at is None for render in carrying):
        try:
            before = (await effective_spec(db, project, user)).spec
        except VideoSpecError:
            before = None
    if project is not None:
        # The video's row locked and read again before its next revision is numbered: two saves of one video in
        # flight together (a batch of scene previews whose lint stores a quad) take their turn, the second reading
        # what the first committed - never two rows of one revision (``uq_video_project_revisions_revision``).
        # Locked after its renders in progress, as a render that ends locks its row then the video's.
        await db.refresh(project, attribute_names=["revision", "spec", "template_container_id"], with_for_update=True)
        if (project.revision or 0) != from_revision:
            if project.template_container_id == template_id and same_spec(project.spec, authored):
                # The save in flight beside this one wrote this very spec: one revision for one change.
                return SavedSpec(
                    project, authored, applied, estimated, notes=notes, unchanged=True, seconds_basis=placed.basis()
                )
            # Another revision landed meanwhile: this one replaces it, and the renders read above were of an older one.
            held = dict(project.spec or {})
            replaced = dict(held)
            music_changed = (
                _music_source(replaced) != _music_source(authored) or project.template_container_id != template_id
            )
            carrying, before = [], None
            from_revision = project.revision or 0
    if project is None:
        project = VideoProject(
            user_id=user.id,
            conversation_id=safe_uuid(str(conversation_id)) if conversation_id else None,
            title=authored["title"],
            spec=authored,
            revision=1,
            scene_count=len(effective["scenes"]),
            estimated_duration_ms=int(estimated * 1000),
            template_container_id=template_id,
            updated_by_user_id=user.id,
        )
        db.add(project)
    else:
        project.spec = authored
        project.title = authored["title"]
        project.revision = (project.revision or 0) + 1
        project.scene_count = len(effective["scenes"])
        project.estimated_duration_ms = int(estimated * 1000)
        project.template_container_id = template_id
        project.updated_by_user_id = user.id
    await db.flush()
    from app.services.video_studio import revisions

    await revisions.record_revision(
        db,
        project_id=project.id,
        revision=project.revision,
        spec=authored,
        previous=held if from_revision else None,
        author_label=getattr(user, "email", None),
        via=via,
        summary=summary,
    )
    await retention.touch(db, replaced, authored)
    # ROI of chat work (#10544): a video built in a conversation is one record, refreshed by every round.
    from app.services.roi_chat_records import record_deliverable, video_minutes

    await record_deliverable(
        db,
        kind="video",
        deliverable_id=project.id,
        conversation_id=project.conversation_id,
        user_id=project.user_id,
        title=project.title,
        size=video_minutes(project.estimated_duration_ms),
    )
    parsed = parse_video_spec(effective)
    # A track shorter than the video is said when it is saved, not heard at the render.
    if length := await music_tracks.length_note(db, parsed.music, estimated):
        notes.append(length)
    # The render it plays may predate the music: a refit keeps the music_id and the revision.
    if played := await render_music_line(db, project, user, parsed, music_changed=music_changed):
        notes.append(played)
    # AI shots made under another template's art direction: advice, read only when there is one to follow.
    if template is not None and template.style is not None and template.style.shots is not None:
        notes.extend(templates.shot_notes(template, await shot_uses(db, effective), video_id=str(project.id)))
    stills = await figure_stills_of(db, user, effective, template_id)
    changed: list[str] = []
    restored: list[str] = []
    kept: list[str] = []
    holding: list[str] = []
    what: dict[str, list[str]] = {}
    if project.scene_previews:
        from app.services.video_studio import lexicon_store, render_checks

        # Hashed as the gate and the render hash them: the platform's lexicon entries under the video's own.
        lexicon = await lexicon_store.for_voiceover(db, parsed.voiceover)
        marks = await render_checks.digest_scenes(db, effective, lexicon)
        # A drawing an earlier release recorded of a scene as it is now counts.
        seen = render_checks.adopted(project.scene_previews, marks)
        changed = render_checks.changed_scenes(seen, marks.whole, marks.content)
        restored = render_checks.restored_scenes(seen, marks.whole)
        kept = render_checks.kept_scenes(seen, marks.whole, marks.content)
        holding = render_checks.holding_scenes(seen, marks.whole, marks.content)
        what = {scene_id: render_checks.changed_parts(seen, scene_id, marks.parts.get(scene_id)) for scene_id in changed}
    if changed:
        listed = ", ".join(
            f"`{scene_id}`" + (f" ({', '.join(what[scene_id])})" if what.get(scene_id) else "") for scene_id in changed
        )
        notes.append(
            f"Changed since their last preview: scene {listed} - preview them again (render_video_scene) before "
            "render_video; the other scenes keep their preview."
        )
    if holding:
        notes.append(holding_line(holding))
    if restored:
        listed = ", ".join(f"`{scene_id}`" for scene_id in restored)
        notes.append(f"Back to how an earlier preview drew them: scene {listed} - that preview counts again.")
    if kept:
        listed = ", ".join(f"`{scene_id}`" for scene_id in kept)
        notes.append(
            f"Only the video's theme or music changed for scene {listed}: each keeps the preview it passed (the "
            "whole render draws and checks the new look)."
        )
    try:
        carried = await carry_renders(db, project, carrying, before=before, after=effective, from_revision=from_revision)
    except Exception as exc:  # a carry that cannot be judged leaves the renders to the next start, as before
        logger.warning("Video %s: its renders in progress were not carried: %s", project.id, format_exception_message(exc))
        carried = []
    if carried:
        await db.flush()
        notes.append(carried_line(carried, project.revision))
    return SavedSpec(
        project,
        authored,
        applied,
        estimated,
        animations,
        notes,
        music_started,
        stills,
        figure_timings_of(planned, effective),
        changed,
        restored,
        preview_holds=holding,
        changed_parts={scene_id: parts for scene_id, parts in what.items() if parts},
        carried=carried,
        seconds_basis=placed.basis(),
    )


#: The most scenes :func:`holding_line` names before it counts the rest.
HOLDING_NAMED = 24


def holding_line(holding: list[str]) -> str:
    """``Previews that still hold: scene `a`, `b` - render_video needs no new preview of them.``: the scenes
    whose passing preview a save left standing (``render_checks.holding_scenes``), said by the save and ``get_video``."""
    listed = ", ".join(f"`{scene_id}`" for scene_id in holding[:HOLDING_NAMED])
    more = f" and {len(holding) - HOLDING_NAMED} more" if len(holding) > HOLDING_NAMED else ""
    return f"Previews that still hold: scene {listed}{more} - render_video needs no new preview of them."


async def preview_holds(db: AsyncSession, project: VideoProject, effective: dict) -> list[str]:
    """The scenes of *project* whose passing preview holds as its *effective* spec is now
    (``render_checks.holding_scenes``), what ``get_video`` lists; none before its first drawing."""
    if not project.scene_previews:
        return []
    from app.services.video_studio import lexicon_store, render_checks

    lexicon = await lexicon_store.for_voiceover(db, parse_video_spec(effective).voiceover)
    marks = await render_checks.digest_scenes(db, effective, lexicon)
    return render_checks.holding_scenes(render_checks.adopted(project.scene_previews, marks), marks.whole, marks.content)


async def effective_spec(
    db: AsyncSession, project: VideoProject, user: User, *, authored: dict | None = None
) -> templates.AppliedTemplate:
    """The project's spec dressed by the template it wears, as *user* may see that template.

    *authored*: another authored spec of the project to dress (a past revision's, ``revisions.spec_at``) instead of
    the one it holds now - what a ready render of that revision is re-judged on (``render_rejudge``).

    A template the reader can no longer see (unshared since) contributes
    nothing, the deck rule (``presentations._load_visible_template``): the
    video reads free-style for them, and the result says so.
    """
    authored = dict(authored if authored is not None else project.spec or {})
    container = None
    missing = False
    if project.template_container_id is not None and not authored.get("freestyle"):
        container = await templates.load_usable_template(db, project.template_container_id, user)
        missing = container is None
    template = await templates.load_video_template(db, container)
    try:
        applied = templates.apply_template(authored, template)
    except VideoSpecError as exc:
        if template is None:
            raise
        # The template changed since the spec was saved, into rules this spec
        # breaks: the video reads without it until it is saved again.
        applied = templates.apply_template(authored, None)
        applied.notes.insert(
            0, f"The {template.name} template no longer applies to this spec ({exc.problems[0].message})."
        )
    if missing:
        applied.notes.insert(0, "The video's template is not shared with you: it reads without it.")
    return applied


async def load_visible_project(db: AsyncSession, project_id: object, user: User) -> VideoProject | None:
    from app.api.v1.helpers import get_user_group_ids, user_visible_filter

    pid = safe_uuid(str(project_id or "").strip())
    if pid is None:
        return None
    groups = await get_user_group_ids(db, user.id)
    result = await db.execute(
        select(VideoProject).where(
            VideoProject.id == pid, user_visible_filter(VideoProject, user.id, groups, is_admin=user.role == "admin")
        )
    )
    return result.scalar_one_or_none()


async def resolve_video_id_prefix(db: AsyncSession, video_id: object, user: User) -> tuple[object, str | None]:
    """*video_id* with an id prefix (8+ hexadecimal characters) of one video
    *user* can open taken for its full id, under :func:`load_visible_project`'s
    own visibility; the refusal naming the candidates when it starts several.

    A full UUID, or anything that is not an id prefix, comes back unchanged
    and reads nothing; a prefix starting no video comes back unchanged, so the
    tool answers its usual "no video you can open".
    """
    from app.api.v1.helpers import get_user_group_ids, user_visible_filter
    from app.services.builtin_tools.id_prefix import id_prefix, resolve_id_prefix

    if id_prefix(video_id) is None:
        return video_id, None
    groups = await get_user_group_ids(db, user.id)
    visible = user_visible_filter(VideoProject, user.id, groups, is_admin=user.role == "admin")
    return await resolve_id_prefix(db, VideoProject, video_id, [visible], noun="videos", label=VideoProject.title)


async def visible_render_file(db: AsyncSession, file_id: object, user: User) -> VideoRender | None:
    """The render whose MP4 *file_id* is, when *user* can open its video; ``None`` otherwise.

    Its MP4 is theirs to look at, as the media route serves it (``_visible_render``): whoever rendered it.
    """
    uid = safe_uuid(str(file_id or ""))
    if uid is None:
        return None
    render = (await db.execute(select(VideoRender).where(VideoRender.file_id == uid).limit(1))).scalars().first()
    if render is None or await load_visible_project(db, render.project_id, user) is None:
        return None
    return render


async def can_manage_project(db: AsyncSession, project: VideoProject, user: User) -> bool:
    from app.api.v1.helpers import can_manage, get_user_group_ids

    return can_manage(user, project, await get_user_group_ids(db, user.id))


class RenderLimitError(Exception):
    """The person already has ``jobs.max_renders_per_user()`` renders of other videos in progress.

    *in_flight* are those renders, oldest first, each with its video's title
    (``jobs.renders_in_flight``): what a refusal lists, to wait for or cancel.
    """

    def __init__(self, in_flight: list[tuple[VideoRender, str]], limit: int) -> None:
        self.in_flight = in_flight
        self.limit = limit
        titles = ", ".join(dict.fromkeys(title for _render, title in in_flight))
        super().__init__(
            f"You already have {len(in_flight)} renders queued or running ({titles}): "
            "wait for one to finish, or cancel one from its video's renders."
        )


@dataclass
class StartedRender:
    render: VideoRender
    #: The renders of the same video it cancelled, oldest first.
    superseded: list[VideoRender]
    #: The render of this revision at this quality already queued or running, answered instead of a new one.
    reused: bool = False
    #: The render of this revision at this quality no worker resumed after its worker stopped, queued again as the
    #: same job, first, from the segments it kept.
    relaunched: bool = False
    #: Where a scene preview stands in its lane of the render queue once queued (``jobs.QueuePlace``).
    queue: Any = None
    #: What a whole render will take, said before it starts (``render_preflight.Preflight``).
    preflight: Any = None
    #: What the pre-render lint wrote before it started (``insert_bezels.settle``): a quad snapped, saved.
    notes: list[str] = field(default_factory=list)
    #: What a scene preview advises (``render_checks.soft_ahead``): a source it draws sharp that a whole render
    #: at a larger quality would refuse as soft. Never a refusal.
    advisories: list[str] = field(default_factory=list)
    #: A render answered (:attr:`reused`) that was asked for an earlier revision and carried to this one, nothing it
    #: draws changed (:func:`carry_renders`): the revision it was asked for.
    carried_from: int | None = None
    #: The ready render answered because it already covers this revision (:func:`covering_render`): the
    #: revision it was drawn for. Nothing was gated, queued or cancelled.
    covered_from: int | None = None
    #: The ready render that draws every scene of this revision as it is now, drawn by an older release, so drawn again
    #: (:func:`covering_render`'s *stale*): what the answer says first.
    redrawn: str | None = None
    #: The scenes of the ready render answered (:attr:`render`, :attr:`covered_from`) that play a fallback take the voice
    #: pipeline or their lexicon changed since (``voice_recheck.Recheck``): a re-check is offered, nothing queued.
    recheck: list = field(default_factory=list)
    #: Why no re-check could be offered on the ready render answered (:func:`covering_render`): the offer could not be
    #: computed from what it recorded, so it is answered as covering the revision, said with this.
    unrechecked: str | None = None
    #: A ready preview of the scene made from the same inputs (``kept_previews.Kept``): every frame cached, the
    #: preview served from it once its start is committed (:func:`run_started`), never dealt to the preview lane.
    kept: Any = None


async def run_started(started: StartedRender) -> None:
    """What a committed start leaves to do: a scene preview whose every frame is cached served from its kept
    preview on this worker (``kept_previews.serve``), any other render handed to the workers (``jobs.wake``)."""
    from app.services.video_studio import jobs, kept_previews

    kept = getattr(started, "kept", None)
    if kept is not None:
        await kept_previews.serve(str(started.render.id), kept)
        return
    await jobs.wake()


async def _lint(
    db: AsyncSession, project: VideoProject, user: User, *, quality: str, scene_id: str | None = None
) -> tuple[dict, list[Any], list[str]]:
    """The pre-render lint of *project*: its screen inserts settled on their footage as it is now
    (``insert_bezels.settle``: a quad written is a new revision, committed at once), then every static fault a render at *quality* -
    of *scene_id* alone for its preview - would find (``render_checks.static_reasons``; a whole render's gate reads
    them with what its previews left). Answers the effective spec drawn, the reasons and the notes."""
    from app.services.audit import log_audit
    from app.services.video_studio import insert_bezels, render_checks

    effective = (await effective_spec(db, project, user)).spec
    settled = await insert_bezels.settle(db, project, user, effective)
    if settled.revision is not None:
        await log_audit(
            db,
            user=user,
            action="update",
            entity_type="video_project",
            entity_id=str(project.id),
            entity_name=project.title,
            summary=f"Snapped the screen inserts of video {project.title} to their measured bezels",
            details={"revision": settled.revision, "scenes": settled.scenes, "via": "pre-render lint"},
        )
        # A whole save, kept whatever the start does next: the quality check reads the revision on a session of its own.
        await db.commit()
        effective = (await effective_spec(db, project, user)).spec
    reasons = [
        render_checks.GateReason("insert_bezel", message, sid)
        for sid, message in settled.problems
        if scene_id is None or sid in (None, scene_id)
    ]
    notes = [note for sid, note in settled.notes if scene_id is None or sid == scene_id]
    if scene_id is not None:
        try:
            reasons.extend(
                await render_checks.static_reasons(db, project, user, effective, quality=quality, scene_id=scene_id)
            )
        except Exception as exc:  # a lint that cannot run never stops a preview: its own checks still run
            logger.warning(
                "Pre-render lint of scene %s of video %s not run: %s", scene_id, project.id, format_exception_message(exc)
            )
    return effective, reasons, notes


# ``query_canceled`` (the statement timeout) and ``lock_not_available`` (a ``lock_timeout``): the wait for a
# render-start lock ran out, nothing else went wrong.
_LOCK_WAIT_EXPIRED = frozenset({"57014", "55P03"})


class RenderStartBusyError(Exception):
    """A render start waited for another start of the same video (or scene) past the statement timeout.

    Said in plain words to the editor and the agent - never the SQL of the
    cancelled wait: nothing was queued, asking again starts it.
    """

    def __init__(self) -> None:
        super().__init__(
            "Another render of this video was being started at the same moment and this start could not wait for it: "
            "nothing was queued. Ask again in a moment."
        )


async def _advisory_lock(db: AsyncSession, scope: str, key: str) -> None:
    """``pg_advisory_xact_lock(hashtext(scope), hashtext(key))``; a wait past the statement timeout raises
    :class:`RenderStartBusyError` instead of the raw cancellation."""
    try:
        await db.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:scope), hashtext(:key))"), {"scope": scope, "key": key}
        )
    except DBAPIError as exc:
        orig = getattr(exc, "orig", None)
        if (getattr(orig, "pgcode", None) or getattr(orig, "sqlstate", None)) in _LOCK_WAIT_EXPIRED:
            raise RenderStartBusyError() from exc
        raise


async def _lock_render_starts(db: AsyncSession, project: VideoProject, user: User) -> None:
    """Serialize the whole render starts of one video, then those of one person.

    The video's lock first, the person's last - always in that order, one of
    each - so two starts never wait on each other in a cycle; the person's
    count of renders in flight is then read and the new render inserted
    before any other start of theirs reads it. A scene preview takes neither
    (:func:`_lock_scene_preview_start`).
    """
    await _advisory_lock(db, "video_render_start", str(project.id))
    await _advisory_lock(db, "video_render_start_user", str(user.id))


async def _lock_scene_preview_start(db: AsyncSession, project: VideoProject, scene_id: str) -> None:
    """Serialize the preview starts of one scene of one video, and nothing else.

    A preview replaces only the preview of its own scene still in progress and
    never counts against the person's whole renders in flight, so the previews
    of a sweep - one call per scene, in one parallel round - never wait on each
    other nor on a whole render's start; two previews of the same scene still
    settle which one replaces the other.
    """
    await _advisory_lock(db, "video_preview_start", f"{project.id}:{scene_id}")


def inherited_conversation(superseded: list[VideoRender], user: User) -> uuid.UUID | None:
    """The conversation owed a render this start replaces, when *user* started it: the new render is owed to it.

    A render started in a chat turn that already ended, then replaced from the
    editor or another tab, would end silently - :func:`jobs.cancel` clears what
    it was owed - and that conversation was promised to hear how it ends.
    Read before the replaced renders are cancelled. Never across
    people: the wake runs as the new render's ``user_id``.
    """
    return next(
        (row.owed_conversation_id for row in superseded if row.owed_conversation_id and row.user_id == user.id),
        None,
    )


def superseded_reason(render_id: object) -> str:
    """What a render replaced by *render_id* says as its error, to people and agents alike."""
    return f"Replaced by a newer render of the same video (`{render_id}`)."


def _revisions(renders: list[VideoRender]) -> str:
    """``revision 99 at 35%`` / ``revisions 98 at 80% and 99 at 35%`` of *renders* in progress."""
    said = [f"{render.revision} at {render.progress or 0}% ({render.quality})" for render in renders]
    if len(said) == 1:
        return f"revision {said[0]}"
    return f"revisions {', '.join(said[:-1])} and {said[-1]}"


def replace_question(older: list[VideoRender], revision: int) -> str:
    """``The render of revision 99 at 35% (ultra) is still in progress: cancel it and render revision 100?``."""
    one = len(older) == 1
    return (
        f"The render{'' if one else 's'} of {_revisions(older)} {'is' if one else 'are'} still in progress: cancel "
        f"{'it' if one else 'them'} and render revision {revision}? The frames {'it' if one else 'they'} drew of the "
        "scenes this revision leaves unchanged are kept for the new render."
    )


def replaced_line(replaced: list[VideoRender], revision: int) -> str:
    """What a start says of the renders in progress it cancelled; the older revisions' frames of its unchanged
    scenes are taken by the new render on the worker (``render_segments.take_over``)."""
    if not replaced:
        return ""
    listed = ", ".join(f"`{render.id}`" for render in replaced)
    older = [render for render in replaced if render.revision < revision]
    if not older:
        return f"This render replaced the render of the video still in progress ({listed}), now cancelled."
    return (
        f"This render of revision {revision} replaced the render of {_revisions(older)} still in progress ({listed}), "
        "now cancelled; the frames it drew of the scenes this revision leaves unchanged are kept for this render, "
        "the rest drawn anew."
    )


class RenderReplaceAskedError(Exception):
    """A start of a person's render that would cancel the render of an older revision still in progress, asked first
    (:func:`replace_question`): they confirm it once (``replace``) or keep the older render drawing."""

    def __init__(self, older: list[VideoRender], revision: int) -> None:
        self.older = older
        self.revision = revision
        super().__init__(replace_question(older, revision))

    def as_dict(self) -> dict:
        return {
            "code": "render_replace",
            "message": str(self),
            "revision": self.revision,
            "renders": [
                {"id": str(render.id), "revision": render.revision, "quality": render.quality, "progress": render.progress or 0}
                for render in self.older
            ],
        }


async def renders_of_older_revisions(db: AsyncSession, project: VideoProject) -> list[VideoRender]:
    """The whole renders of revisions before *project*'s current one still queued or running, oldest first."""
    statement = (
        select(VideoRender)
        .where(
            VideoRender.project_id == project.id,
            VideoRender.revision < project.revision,
            VideoRender.scene_id.is_(None),
            VideoRender.status.in_((JOB_PENDING, JOB_RUNNING)),
        )
        .order_by(VideoRender.created_at)
    )
    return list((await db.execute(statement)).scalars().all())


async def render_in_flight(
    db: AsyncSession, project: VideoProject, *, quality: str, lock: bool = False
) -> VideoRender | None:
    """The whole render of *project*'s current revision at *quality* still queued or running, if any.

    A render the worker lost the lease of and runs again is the same row, so
    it is this one too: a start never queues its twin, and the render list
    shows it once.
    """
    statement = (
        select(VideoRender)
        .where(
            VideoRender.project_id == project.id,
            VideoRender.revision == project.revision,
            VideoRender.quality == quality,
            VideoRender.scene_id.is_(None),
            VideoRender.status.in_((JOB_PENDING, JOB_RUNNING)),
        )
        .order_by(VideoRender.created_at.desc())
        .limit(1)
    )
    if lock:
        statement = statement.with_for_update()
    found = (await db.execute(statement)).scalars().all()
    return found[0] if found else None


async def renders_to_carry(db: AsyncSession, project: VideoProject) -> list[VideoRender]:
    """The whole renders of *project*'s current revision still queued or running, oldest first, locked: what a
    save of its next revision may carry (:func:`carry_renders`)."""
    statement = (
        select(VideoRender)
        .where(
            VideoRender.project_id == project.id,
            VideoRender.revision == project.revision,
            VideoRender.scene_id.is_(None),
            VideoRender.status.in_((JOB_PENDING, JOB_RUNNING)),
        )
        .order_by(VideoRender.created_at)
        .with_for_update()
    )
    return list((await db.execute(statement)).scalars().all())


@dataclass(frozen=True)
class CarriedRender:
    """A whole render in progress carried to the revision a save made (:func:`carry_renders`)."""

    render: VideoRender
    #: The revision it was asked for, the first one it was carried from.
    from_revision: int


async def carry_renders(
    db: AsyncSession,
    project: VideoProject,
    renders: list[VideoRender],
    *,
    before: dict | None,
    after: dict,
    from_revision: int,
) -> list[CarriedRender]:
    """Carry to *project*'s new revision those of *renders* - its whole renders of *from_revision* still in progress
    (:func:`renders_to_carry`) - that draw exactly what it draws, instead of leaving them to be cancelled and
    drawn again by the next :func:`start_render`.

    What a render draws is the digest of every scene's frames' inputs (``render_checks.SceneDigests.drawn``: nothing
    heard, a deck without its speaker notes), with the sources of its music (``render_checks.sound_sources``). One
    that started compares what it recorded as it read its spec (``VideoRender.drawing``) with the *after* spec's; a
    queued one, which reads the spec when it starts, compares the *before* spec's - the revision it was asked for. A
    render that started without recording it is not carried. A carried render is the render of the new revision -
    ``start_render`` answers it -, records the *after* spec it mixes that revision's sound from (a clip's audio, the
    levels) over the frames it drew, and the scene digests it records once ready.
    """
    from app.services.video_studio import lexicon_store, render_checks

    if not renders:
        return []

    async def digests(effective: dict) -> render_checks.SceneDigests:
        lexicon = await lexicon_store.for_voiceover(db, parse_video_spec(effective).voiceover)
        return await render_checks.digest_scenes(db, effective, lexicon)

    now = await digests(after)
    sources = render_checks.sound_sources(after)
    queued_matches = False
    if before is not None and any(render.started_at is None for render in renders):
        then = await digests(before)
        queued_matches = bool(now.drawn) and then.drawn == now.drawn and render_checks.sound_sources(before) == sources
    def draws_the_same(render: VideoRender, drawing: dict | None) -> bool:
        if render.started_at is None:
            return queued_matches
        return (
            drawing is not None
            and drawing.get("revision") == from_revision
            and drawing.get("scenes") == now.drawn
            and drawing.get("sources") == sources
        )

    carried: list[CarriedRender] = []
    for render in renders:
        drawing = render.drawing if isinstance(render.drawing, dict) else None
        if not draws_the_same(render, drawing):
            continue
        first = int((drawing or {}).get("carried_from") or from_revision)
        render.revision = project.revision
        if drawing is not None:
            render.drawing = {
                **drawing,
                "revision": project.revision,
                "carried_from": first,
                "spec": after,
                "whole": now.whole,
                "content": now.content,
                # What each scene is made of at that revision: what a later refusal names.
                "parts": now.parts,
                # It mixes the sound of the revision it is carried to: what it plays is that revision's.
                "heard": now.heard,
                "said": now.said,
                "beside": now.beside,
                "picture": now.picture,
            }
        carried.append(CarriedRender(render, first))
    return carried


def carried_line(carried: list[CarriedRender], revision: int) -> str:
    """``Kept: the high render in progress (34%) draws exactly what revision 13 draws ...``; empty for none."""
    if not carried:
        return ""
    listed = ", ".join(f"`{item.render.id}` at {item.render.quality} ({item.render.progress or 0}%)" for item in carried)
    one = len(carried) == 1
    return (
        f"Kept: the render{'' if one else 's'} in progress ({listed}) {'draws' if one else 'draw'} exactly what revision "
        f"{revision} draws - this edit changes nothing on screen -, so {'it is' if one else 'they are'} carried to "
        f"revision {revision} and {'finishes' if one else 'finish'} as its render, the sound mixed for it, instead of "
        "being cancelled and drawn again; render_video answers it."
    )


async def render_left_unresumed(db: AsyncSession, project: VideoProject, *, quality: str) -> VideoRender | None:
    """The whole render of *project*'s current revision at *quality* no worker resumed after its worker stopped, a
    relaunch may still bring back (``jobs.relaunchable``), locked; the newest when several are."""
    from app.models.orchestration import OrchestrationExecution
    from app.services.video_studio import jobs

    found = (
        (
            await db.execute(
                select(VideoRender)
                .join(OrchestrationExecution, OrchestrationExecution.id == VideoRender.execution_id)
                .where(
                    VideoRender.project_id == project.id,
                    VideoRender.revision == project.revision,
                    VideoRender.quality == quality,
                    jobs.relaunchable(),
                )
                .order_by(VideoRender.finished_at.desc())
                .limit(1)
                .with_for_update(of=VideoRender)
            )
        )
        .scalars()
        .all()
    )
    return found[0] if found else None


def _reused(render: VideoRender, owed_to: uuid.UUID | None) -> StartedRender:
    """*render* answered to a start that would have made its twin; owed to the asking chat if nobody is told of it."""
    if owed_to is not None and render.owed_conversation_id is None:
        render.owed_conversation_id = owed_to
    return StartedRender(render, [], reused=True, carried_from=carried_from(render))


def carried_from(render: VideoRender) -> int | None:
    """The revision *render* was asked for when a later one that changes nothing it draws carried it."""
    drawing = render.drawing if isinstance(getattr(render, "drawing", None), dict) else {}
    first = drawing.get("carried_from")
    return int(first) if isinstance(first, int) and first != render.revision else None


def _covered_as_before(drawing: dict, marks: Any) -> list | None:
    """Whether a ready render covers the revision by its digests alone (``[]``: everything drawn and heard the same) when
    its re-check offer could not be computed; ``None`` otherwise."""
    return [] if drawing.get("scenes") == marks.drawn and drawing.get("heard") == marks.heard else None


async def covering_render(
    db: AsyncSession,
    project: VideoProject,
    user: User,
    *,
    quality: str,
    stale: list[str] | None = None,
    offered: list | None = None,
    unrechecked: list[str] | None = None,
) -> tuple[VideoRender, int] | None:
    """The video's latest ready whole render, when it already covers *project*'s current revision at *quality*,
    with the revision it was the render of: what a start of that render answers instead of drawing it
    again, locked and taken as the current revision's render. ``None`` otherwise.

    It covers it when it was drawn at *quality*, its checks left no finding (``render_answer``: all gates green),
    and the revision draws and plays exactly what it recorded (``VideoRender.drawing``): every scene's drawn digest
    and what it plays beside its frames (``render_checks.SceneDigests.drawn`` and ``heard``) - so a legibility
    declaration or a deck's speaker notes changed nothing, a scene's trim or its own sound did. A render that
    recorded neither (made by an older release) covers nothing. Nor does one this release would draw or judge otherwise
    (``render_checks.stale_release``): its drawing version or its insert build version is not the current one, or its
    checks were judged under an older quality report version than the running one (or recorded none) - a fix to how
    whole-render frames are drawn moves that version with the gate that reads the defect, so a render made before it
    never escapes that gate - said in *stale* (``drawn at screen-insert drawing version 7, now 8``, ``judged under
    quality report v48, now v49``: rendering again), and it is drawn again, no *force* needed.

    Nor does one playing a fallback take whose reuse digest moved since (``voice_recheck.offered``): the voice
    pipeline changed, or the lexicon entries its narration writes - everything else drawn and heard the same, the
    narrations' lengths aside. It is put in *offered* (``voice_recheck.Offer``: that render, its scenes to re-check),
    untouched, and ``None`` is answered: :func:`start_render` answers it with the offer, nothing queued. An older render
    recorded no fallback: its scenes playing one are read from its voice summaries and the voice cache's
    check records (``voice_recheck.records_of``). An offer that cannot be computed never stops the start: the render
    covers the revision without an offer, why no re-check was offered put in *unrechecked* and logged.
    """
    from app.services.video_studio import lexicon_store, render_answer, render_checks, voice_recheck

    found = (
        (
            await db.execute(
                select(VideoRender)
                .where(
                    VideoRender.project_id == project.id,
                    VideoRender.scene_id.is_(None),
                    VideoRender.status == JOB_READY,
                )
                .order_by(VideoRender.finished_at.desc().nulls_last(), VideoRender.created_at.desc())
                .limit(1)
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    latest = found[0] if found else None
    drawing = latest.drawing if latest is not None and isinstance(latest.drawing, dict) else None
    if latest is None or latest.quality != quality or not drawing or not drawing.get("scenes") or not drawing.get("heard"):
        return None
    if render_answer.of_render(latest, latest.checks).status != render_answer.GREEN:
        return None
    effective = (await effective_spec(db, project, user)).spec
    lexicon = await lexicon_store.for_voiceover(db, parse_video_spec(effective).voiceover)
    marks = await render_checks.digest_scenes(db, effective, lexicon)
    try:
        records = None
        if not isinstance(drawing.get("fallbacks"), dict):
            # An older render: what its takes heard wrong is in the voice cache, never on the render.
            records = await voice_recheck.records_of(db, user, effective, lexicon)
        rechecks = voice_recheck.offered(drawing, latest.checks, marks, voice_recheck.line_speeds(effective), records)
    except Exception as exc:  # an offer that cannot be computed leaves the render covering the revision as before
        logger.warning(
            "Re-check offer on render %s of video %s not computed: %s",
            latest.id,
            project.id,
            format_exception_message(exc),
        )
        if unrechecked is not None:
            unrechecked.append(
                f"its scenes playing a fallback take could not be re-checked ({format_exception_message(exc)[:200]})"
            )
        rechecks = _covered_as_before(drawing, marks)
    if rechecks is None:
        return None
    if (why := render_checks.stale_release(drawing, latest.checks if isinstance(latest.checks, dict) else {})) is not None:
        if stale is not None:
            stale.append(
                f"Render `{latest.id}` draws every scene of revision {project.revision} at {quality} as it is now, but "
                f"was {why}: rendering again."
            )
        return None
    if rechecks:
        if offered is not None:
            offered.append(voice_recheck.Offer(latest, rechecks))
        return None
    before = latest.revision
    if before != project.revision:
        # The render of this revision now (``rendered_seconds``, the render lists): what it draws and plays is this
        # revision's.
        latest.drawing = {**drawing, "revision": project.revision, "drawn_for": before, "whole": marks.whole}
        latest.revision = project.revision
    return latest, before


async def start_render(
    db: AsyncSession,
    project: VideoProject,
    user: User,
    *,
    quality: str,
    owed_to: uuid.UUID | None = None,
    via_agent: bool = False,
    force: bool = False,
    ask_before_replacing: bool = False,
    splice: Any = None,
) -> StartedRender:
    """Queue a render of *project* as *user*, replacing the video's renders in progress.

    The one way a render starts, from the editor and from ``render_video``
    alike. A start asking what is already being made - the current
    revision at the same quality, queued or running - answers that render
    (``reused``): nothing is cancelled, queued, gated or audited
    again. One asking what the video's latest ready render already covers -
    drawn at that quality, its gates green, the revision drawing and playing
    exactly what it did (:func:`covering_render`) - answers that render
    (``covered_from``): nothing to draw, so nothing is queued, unless
    *force* asks to draw it again. One asking a render of that revision and quality no worker
    resumed after its worker stopped (:func:`render_left_unresumed`) brings
    that render back once it passed the gate below (``relaunched``):
    the same job, queued again first, resuming from the segments it kept.
    Otherwise its screen inserts are first settled on their footage as
    it is now (:func:`_lint`: a quad snapped is a new revision, said in
    :attr:`StartedRender.notes`), then it passes the quality gate
    (``render_checks.gate``), the pre-render lint of every static fault
    among its reasons: a narration that failed its check,
    findings the previous render of this revision left open, a scene changed
    since its preview refuse it (``render_checks.RenderGateError``) - and so
    does what the pre-flight finds would stop it on the worker
    (``render_preflight``), whose estimate the start answers
    (:attr:`StartedRender.preflight`); *force* starts it anyway, the override
    kept on the render, logged and audited. A
    video has one render in progress at most: every render of it still
    queued or running is cancelled - a running one stops between frames, a
    queued one never starts - and records the render that replaced it
    (``superseded_by_id``). The starts of one video are serialized, so two
    racing starts never both miss each other, and the person's limit of
    renders in flight (``jobs.max_renders_per_user``) counts the replaced
    renders out (:class:`RenderLimitError`). The caller has checked that
    *user* manages the video and that *quality* is a preset; it commits, then
    calls ``jobs.wake()``.

    A render of an older revision still in progress is the render the
    person is waiting on: once the gate passed, a start with
    *ask_before_replacing* (the editor's, until they confirm) raises
    :class:`RenderReplaceAskedError` instead of cancelling it; an agent's
    start cancels it and says so (:func:`replaced_line`). Either way the
    replaced render hands over its segments, and the new one takes those of
    the scenes its revision leaves unchanged (``render_segments.take_over``).

    A ready render covering the picture whose scenes playing a fallback take
    moved since (:func:`covering_render`'s *offered*) is answered with
    the re-check offered (:attr:`StartedRender.recheck`), nothing queued. A
    *splice* (``voice_recheck.Splice``: the re-check ran, some now pass at the
    video's speed) skips that answer and queues a whole render recording it
    (``VideoRender.drawing["splice"]``): the worker keeps that ready render's
    frames and other takes (``render._run_render``).
    """
    from app.services.audit import log_audit
    from app.services.video_studio import jobs, render_checks, render_preflight, voice_recheck

    if (running := await render_in_flight(db, project, quality=quality)) is not None:
        return _reused(running, owed_to)
    stale: list[str] = []
    offered: list = []
    unrechecked: list[str] = []
    if not force and splice is None:
        covering = await covering_render(
            db, project, user, quality=quality, stale=stale, offered=offered, unrechecked=unrechecked
        )
        if covering is not None:
            render, drawn_for = covering
            logger.info(
                "Video render of %s not queued: render %s (revision %d) covers revision %d at %s", project.id, render.id, drawn_for, project.revision, quality
            )
            return StartedRender(
                render, [], covered_from=drawn_for, unrechecked=unrechecked[0] if unrechecked else None
            )
        if offered:
            found = offered[0]
            logger.info(
                "Video render of %s not queued: render %s covers its picture, a re-check of %s offered",
                project.id,
                found.render.id,
                ", ".join(recheck.scene_id for recheck in found.rechecks),
            )
            return StartedRender(found.render, [], covered_from=found.render.revision, recheck=list(found.rechecks))
    effective, reasons, notes = await _lint(db, project, user, quality=quality)
    reasons = [*reasons, *await render_checks.gate(db, project, user, effective, quality=quality)]
    if reasons and notes:
        # Said where the render is refused: what the lint wrote changed the scenes it names.
        reasons = [*(render_checks.GateReason(render_checks.SNAPPED, note) for note in notes), *reasons]
    # What the render will take, against the worker that draws it: what would stop it is a reason too.
    try:
        estimate = await render_preflight.preflight(db, effective, quality=quality, video_id=project.id, user=user)
    except Exception as exc:  # an estimate that cannot be made never stops a render
        logger.warning("Video render pre-flight of %s not made: %s", project.id, format_exception_message(exc))
        estimate = None
    if estimate is not None and estimate.refuses:
        reasons = [*reasons, *(render_checks.GateReason("preflight", problem.rstrip(".")) for problem in estimate.problems)]
    if reasons and not force:
        raise render_checks.RenderGateError(reasons)
    if ask_before_replacing and (older := await renders_of_older_revisions(db, project)):
        raise RenderReplaceAskedError(older, project.revision)
    # Only a refusal overridden is an override: force with nothing to refuse leaves no trace of a bypass.
    override = None
    if force and reasons:
        override = {
            "by": str(user.id),
            "at": datetime.now(UTC).isoformat(),
            "via": "agent" if via_agent else "editor",
            "reasons": [reason.as_dict() for reason in reasons],
        }
        logger.warning(
            "Video render of %s forced past its quality gate by %s (%d reason(s)): %s",
            project.id,
            user.id,
            len(reasons),
            "; ".join(reason.message for reason in reasons),
        )
    await _lock_render_starts(db, project, user)
    # Read again under the lock: a twin started while the gate ran is the one answered.
    if (running := await render_in_flight(db, project, quality=quality, lock=True)) is not None:
        return _reused(running, owed_to)
    in_progress = (JOB_PENDING, JOB_RUNNING)
    # Locked: a render finishing meanwhile is read as finished and kept. A scene preview is not replaced.
    superseded = list(
        (
            await db.execute(
                select(VideoRender)
                .where(
                    VideoRender.project_id == project.id,
                    VideoRender.status.in_(in_progress),
                    VideoRender.scene_id.is_(None),
                )
                .order_by(VideoRender.created_at)
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    elsewhere = [
        (row, title) for row, title in await jobs.renders_in_flight(db, user.id) if row.project_id != project.id
    ]
    if len(elsewhere) >= jobs.max_renders_per_user():
        raise RenderLimitError(elsewhere, jobs.max_renders_per_user())
    owed = owed_to or inherited_conversation(superseded, user)
    # Said by get_video_jobs while it runs and beside what it measured once drawn.
    preflight = estimate.as_dict() if estimate is not None else None
    render = await render_left_unresumed(db, project, quality=quality)
    relaunched = render is not None
    if render is not None:
        render.status, render.error, render.finished_at, render.progress_at = JOB_PENDING, None, None, None
        render.stage = f"Queued again first: resumes from the segments it kept at {render.progress or 0}%"
        render.gate_override, render.preflight, render.superseded_by_id = override, preflight, None
        # Drawn as the person who asks again, like any render they start.
        render.user_id = user.id
        await jobs.enqueue_again(db, render, owed_to=owed)
    else:
        render = VideoRender(
            project_id=project.id,
            user_id=user.id,
            revision=project.revision,
            quality=quality,
            stage="Queued",
            gate_override=override,
            preflight=preflight,
            # What it splices into, read by the worker before it draws.
            drawing={voice_recheck.SPLICE: splice.as_dict()} if splice is not None else None,
        )
        db.add(render)
        await db.flush()
        await jobs.enqueue(db, "render", render, owed_to=owed)
    for previous in superseded:
        await jobs.cancel(db, "render", previous, reason=superseded_reason(render.id))
        previous.superseded_by_id = render.id
    details: dict[str, Any] = {"quality": quality, "revision": project.revision}
    if superseded:
        details["superseded_render_ids"] = [str(r.id) for r in superseded]
    if via_agent:
        details["via"] = "agent"
    if override is not None:
        details["forced"] = True
        details["gate_reasons"] = override["reasons"]
    if relaunched:
        details["resumes_kept_segments_of"] = str(render.id)
    elif splice is not None:
        details["splices_into"] = splice.source
        details["rechecked_scenes"] = list(splice.scenes)
    await log_audit(
        db,
        user=user,
        action="trigger",
        entity_type="video_project",
        entity_id=str(project.id),
        entity_name=project.title,
        summary=f"Rendered video {project.title} ({quality})" + (" past its quality gate" if override else ""),
        details=details,
    )
    return StartedRender(
        render, superseded, preflight=estimate, notes=notes, relaunched=relaunched, redrawn=stale[0] if stale else None
    )


def preview_quality(effective: dict, scene_id: str) -> tuple[str, str | None]:
    """The quality a preview of *scene_id* is drawn at when none is asked, and why it is not a draft.

    A figure, a recording or a screen insert is judged on its sharpness:
    ``standard`` (1080p), so what the player scales up to the display is
    what a render shows. Any other scene is a ``draft`` (720p).
    """
    scene = next((s for s in effective.get("scenes") or [] if isinstance(s, dict) and s.get("id") == scene_id), None) or {}
    if scene.get("type") in ("figure", "recording"):
        return "standard", f"it shows a {scene['type']}"
    if any(isinstance(overlay, dict) and overlay.get("type") == "screen_insert" for overlay in scene.get("overlays") or []):
        return "standard", "it shows a screen insert"
    return "draft", None


def resolve_scene(authored: dict, effective: dict, scene_ref: object) -> str:
    """The id of the scene *scene_ref* names: an index of the spec as written (what an author reads) or an id.

    An id may also be a scene the template adds (its opening or closing card).
    """
    written = [scene for scene in authored.get("scenes") or [] if isinstance(scene, dict)]
    if isinstance(scene_ref, int) or (isinstance(scene_ref, str) and scene_ref.strip().isdigit()):
        index = int(scene_ref)
        if 0 <= index < len(written) and written[index].get("id"):
            return str(written[index]["id"])
        raise VideoSpecError(
            [SpecProblem("scene", f"no scene {index}: the video's scenes are 0 to {len(written) - 1}")]
        )
    ids = [scene.get("id") for scene in effective.get("scenes") or [] if isinstance(scene, dict)]
    if isinstance(scene_ref, str) and scene_ref.strip() in ids:
        return scene_ref.strip()
    raise VideoSpecError([SpecProblem("scene", f"no scene {scene_ref!r} (give its id or its 0-based index)")])


async def start_scene_render(
    db: AsyncSession,
    project: VideoProject,
    user: User,
    *,
    scene: object,
    quality: str | None = None,
    owed_to: uuid.UUID | None = None,
    via_agent: bool = False,
    force: bool = False,
) -> StartedRender:
    """Queue a preview of one scene of *project* as *user*: a short render, in the preview lane.

    First the pre-render lint of that scene (:func:`_lint`): its
    screen inserts settled on their footage, then every static fault its
    preview would find - refused with the whole list
    (``render_checks.RenderGateError``) unless *force*, as a whole render is.
    A source it draws sharp that a whole render at a larger quality would
    refuse as soft is said (:attr:`StartedRender.advisories`).

    Only that scene is drawn, with its voice, its music and its transitions
    in and out (``render.scene_window``), at *quality* - one of
    ``spec.PREVIEW_QUALITIES`` -, else at the one :func:`preview_quality`
    gives the scene (1080p for a figure, a recording or a screen
    insert, a 720p draft otherwise). A
    preview replaces the preview of the same scene still in progress, never a
    whole render, and never becomes what the video plays. It runs in the
    preview lane, on slots of its own beside the whole renders. Every
    preview asked is queued: a video has one preview in flight per
    scene at most, and ``jobs.max_scene_previews_per_video`` bounds how many
    of them run at once (``jobs.preview_waits``), never how many may be
    asked - a sweep of every scene is one batch of calls: each start locks
    only its own scene (:func:`_lock_scene_preview_start`), so the calls of
    one parallel round never wait on each other, and a wait that still runs
    out raises :class:`RenderStartBusyError`, never the SQL. The lane's slots
    claim one preview per video in turn (``preview_lane.deal_claims``):
    a sweep of twenty scenes never waits behind every preview of another film;
    :attr:`StartedRender.queue` says where the new one stands and behind which
    videos' previews. A preview made from the same inputs as a ready preview
    of the scene (``kept_previews``) has every frame cached: it is
    never dealt to the lane, :attr:`StartedRender.kept` names the preview it
    is served from on the worker that asked, its job held behind
    ``kept_previews.SERVE_GRACE_SECONDS`` should that serve not finish. The
    caller has checked that *user* manages the video; it commits, then calls
    :func:`run_started`. Raises :class:`VideoSpecError` for a scene the video
    does not have, or a quality a preview is not drawn at.
    """
    from app.services.audit import log_audit
    from app.services.video_studio import jobs, kept_previews
    from app.services.video_studio.spec import PREVIEW_QUALITIES

    if quality is not None and quality not in PREVIEW_QUALITIES:
        raise VideoSpecError([SpecProblem("quality", f"a scene preview is drawn at {' or '.join(PREVIEW_QUALITIES)}")])
    from app.services.video_studio import render_checks

    applied = await effective_spec(db, project, user)
    scene_id = resolve_scene(dict(project.spec or {}), applied.spec, scene)
    drawn_at = quality or preview_quality(applied.spec, scene_id)[0]
    effective, reasons, notes = await _lint(db, project, user, quality=drawn_at, scene_id=scene_id)
    if reasons and not force:
        raise render_checks.RenderGateError(
            [*(render_checks.GateReason(render_checks.SNAPPED, note, scene_id) for note in notes), *reasons]
        )
    # What a whole render at a larger quality would refuse that this preview draws sharp: said now, not after
    # every scene passed its preview.
    advisories = await render_checks.soft_ahead(db, effective, scene_id=scene_id, quality=drawn_at)
    # What it is made from: a ready preview made from the same has every frame of this one cached. Read before the
    # lock: it reads ready previews only, and the previews of a sweep compute it side by side.
    made_from = await kept_previews.inputs_or_none(
        db, effective, getattr(applied, "template", None), scene_id=scene_id, quality=drawn_at, user=user
    )
    source = (
        await kept_previews.kept(db, project.id, scene_id=scene_id, quality=drawn_at, made_from=made_from)
        if made_from is not None
        else None
    )
    # Its own scene's lock, held only for the replace and the insert: the previews of the other scenes never wait.
    await _lock_scene_preview_start(db, project, scene_id)
    in_progress = (JOB_PENDING, JOB_RUNNING)
    superseded = list(
        (
            await db.execute(
                select(VideoRender)
                .where(
                    VideoRender.project_id == project.id,
                    VideoRender.status.in_(in_progress),
                    VideoRender.scene_id == scene_id,
                )
                .order_by(VideoRender.created_at)
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    render = VideoRender(
        project_id=project.id,
        user_id=user.id,
        revision=project.revision,
        quality=drawn_at,
        stage="Queued",
        scene_id=scene_id,
    )
    db.add(render)
    await db.flush()
    held = datetime.now(UTC) + timedelta(seconds=kept_previews.SERVE_GRACE_SECONDS) if source is not None else None
    await jobs.enqueue(db, "render", render, owed_to=owed_to or inherited_conversation(superseded, user), not_before=held)
    for previous in superseded:
        await jobs.cancel(db, "render", previous, reason=superseded_reason(render.id))
        previous.superseded_by_id = render.id
    await db.flush()
    queue = await jobs.queue_place("render", render, db=db) if source is None else None
    details: dict[str, Any] = {"scene_id": scene_id, "revision": project.revision, "quality": drawn_at}
    if source is not None:
        details["served_from_render_id"] = source.render_id
    if superseded:
        details["superseded_render_ids"] = [str(r.id) for r in superseded]
    if via_agent:
        details["via"] = "agent"
    if force and reasons:
        details["forced"] = True
        details["gate_reasons"] = [reason.as_dict() for reason in reasons]
    await log_audit(
        db,
        user=user,
        action="trigger",
        entity_type="video_project",
        entity_id=str(project.id),
        entity_name=project.title,
        summary=f"Previewed scene {scene_id} of video {project.title} ({drawn_at})" + (" past its lint" if force and reasons else ""),
        details=details,
    )
    return StartedRender(render, superseded, queue=queue, notes=notes, advisories=advisories, kept=source)


async def latest_renders(db: AsyncSession, project_ids: list[uuid.UUID]) -> dict[str, VideoRender]:
    """The newest whole render of each project (ready or not): a scene preview is never one."""
    if not project_ids:
        return {}
    rows = await db.execute(
        select(VideoRender)
        .where(VideoRender.project_id.in_(project_ids), VideoRender.scene_id.is_(None))
        .order_by(VideoRender.created_at.desc())
    )
    latest: dict[str, VideoRender] = {}
    for render in rows.scalars().all():
        latest.setdefault(str(render.project_id), render)
    return latest


def serialize_render(
    render: VideoRender | None,
    *,
    project: VideoProject | None = None,
    standing: render_retention.Standing | None = None,
) -> dict | None:
    """A render; with *project*, whether it is the current one; with its video's *standing* too
    (``render_retention.standing``), what keeps it and when the retention may delete it.
    """
    from app.services.video_studio import render_pace, render_segments

    if render is None:
        return None
    kept_until = render_segments.frames_kept_until(render)
    retention_fields = (
        {
            "is_current": project.latest_render_id == render.id,
            "kept_by": render_retention.kept_by(render, standing) if standing is not None else None,
            "expires_at": (
                due.isoformat()
                if standing is not None and (due := render_retention.expires_at(render, standing))
                else None
            ),
        }
        if project is not None
        else {}
    )
    return {
        "id": str(render.id),
        "project_id": str(render.project_id),
        "revision": render.revision,
        "quality": render.quality,
        "status": render.status,
        "progress": render.progress,
        "stage": render.stage,
        "width": render.width,
        "height": render.height,
        "fps": render.fps,
        "duration_ms": render.duration_ms,
        "size_bytes": render.size_bytes,
        "file_id": str(render.file_id) if render.file_id else None,
        "poster_file_id": str(render.poster_file_id) if render.poster_file_id else None,
        "captions_file_id": str(render.captions_file_id) if render.captions_file_id else None,
        "captions_burned_in": render.captions_burned_in is not False,
        "notes": list(render.notes or []),
        "keep": bool(render.keep),
        "user_id": str(render.user_id) if render.user_id else None,
        "error": render.error,
        "superseded_by_id": str(render.superseded_by_id) if render.superseded_by_id else None,
        #: The revision it was asked for, when a later one that changes nothing it draws carried it.
        "carried_from_revision": carried_from(render),
        #: A draft preview of this scene, never what the video plays; ``None`` for a whole render.
        "scene_id": render.scene_id,
        "frame_file_ids": [str(fid) for fid in render.frame_file_ids or []],
        #: Where each scene takes over, and its name; empty for a scene preview and older renders.
        "chapters": list(render.chapters or []),
        #: The music it played: ``{kind, ref, file_id?}``; ``None`` for the renders made before it.
        "music": dict(render.music) if isinstance(getattr(render, "music", None), dict) else None,
        #: What its audio and picture checks found, each finding with its time; ``None`` before them.
        "checks": render.checks,
        #: Started past its quality gate (``force``).
        "forced": bool(render.gate_override),
        #: The times it stood still and was resumed in place, its finished frames kept.
        "stalls": list(getattr(render, "stalls", None) or []),
        #: Each time another worker took it over: the attempt, what it resumed after and the frame it resumed from.
        "relaunches": list(getattr(render, "relaunches", None) or []),
        #: A failed render's frames kept for the next render of its revision: until when, ``None`` otherwise.
        "frames_kept_until": kept_until.isoformat() if kept_until else None,
        #: When an unfinished whole render should end: as its pre-flight estimated, then at the pace its frames
        #: measured once a tenth of them are drawn; ``None`` for a scene preview or a finished render.
        "eta": render_pace.eta_of(render),
        "created_at": render.created_at.isoformat() if render.created_at else None,
        "started_at": render.started_at.isoformat() if render.started_at else None,
        "progress_at": render.progress_at.isoformat() if render.progress_at else None,
        "finished_at": render.finished_at.isoformat() if render.finished_at else None,
        **retention_fields,
    }


def serialize_template_ref(container: Any | None) -> dict | None:
    if container is None:
        return None
    return {"id": str(container.id), "name": container.name, "kind": container.kind}


def video_permission(project: VideoProject, user: User, *, can_manage: bool) -> str:
    """The reader's tier on a video they can open: ``owner``, ``config`` (may edit) or ``read_only``."""
    if project.user_id is not None and project.user_id == user.id:
        return "owner"
    return "config" if can_manage else "read_only"


def serialize_project(
    project: VideoProject,
    *,
    can_manage: bool,
    latest: VideoRender | None = None,
    ready: VideoRender | None = None,
    template: Any | None = None,
    applied: templates.AppliedTemplate | None = None,
    permission: str | None = None,
    owner_name: str | None = None,
    has_public_link: bool = False,
) -> dict:
    """A project; with *applied* (a detail read), the effective spec and what the template changed.

    The sharing fields are what the Share dialog edits: every grant
    with its tier, the restriction of a company-managed video, the reader's
    own tier, the owner's name and whether a public link plays it now.
    """
    payload = {
        "id": str(project.id),
        "user_id": str(project.user_id) if project.user_id else None,
        "title": project.title,
        #: The chat that made it: the editor offers to continue it.
        "conversation_id": str(project.conversation_id) if project.conversation_id else None,
        "spec": project.spec,
        "template": serialize_template_ref(template),
        "freestyle": bool((project.spec or {}).get("freestyle")),
        "revision": project.revision,
        "scene_count": project.scene_count,
        "estimated_duration_ms": project.estimated_duration_ms,
        "latest_render": serialize_render(latest),
        "ready_render": serialize_render(ready),
        "is_company_managed": bool(project.is_company_managed),
        "shared_group_ids": list(project.shared_group_ids or []),
        "shared_group_permissions": dict(project.shared_group_permissions or {}),
        "shared_user_ids": list(project.shared_user_ids or []),
        "shared_user_permissions": dict(project.shared_user_permissions or {}),
        "access_restricted": bool(project.access_restricted),
        "permission": permission,
        "owner_name": owner_name,
        "has_public_link": has_public_link,
        "can_manage": can_manage,
        "created_at": project.created_at.isoformat() if project.created_at else None,
        "updated_at": project.updated_at.isoformat() if project.updated_at else None,
    }
    if applied is not None:
        payload["effective_spec"] = applied.spec
        payload["template_scenes"] = list(applied.added_scenes)
        payload["template_notes"] = list(applied.notes)
    return payload
