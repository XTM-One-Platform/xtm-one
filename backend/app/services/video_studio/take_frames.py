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

"""A frame of a recording as a picture of a video: a statement's backdrop, an image scene.

Either names ``{take_id, at_step | at_seconds}`` (``spec.RecordingFrame``)
instead of a ``file_id``: the save (:func:`resolve`) takes that frame once -
after a step, the frame the recording's contact sheet shows for it
(``clips.resolve_frame``, as ``save_video_frame`` does) - keeps it as a
picture of the person, and writes its ``file_id`` beside the recording with
the moment it holds (``frame_of``, :func:`frame_key`). A save that names the
same moment again - this revision or the one it replaces - keeps that
picture; another moment or another recording takes the frame again.
"""

from __future__ import annotations

import copy
import tempfile
from collections.abc import Iterator
from typing import Any

from app.services.video_studio.spec import PICTURE_SOURCES, SpecProblem, VideoSpecError


def frame_key(holder: dict) -> str | None:
    """The moment of a recording *holder* (a backdrop, an image scene) names: ``take:<id>:step:14``; ``None`` for
    none."""
    take = holder.get("take_id")
    if not take:
        return None
    if holder.get("at_step") is not None:
        return f"take:{take}:step:{int(holder['at_step'])}"
    if holder.get("at_seconds") is not None:
        return f"take:{take}:at:{float(holder['at_seconds']):g}"
    return f"take:{take}:last"


def _moment(holder: dict) -> str:
    if holder.get("at_step") is not None:
        return f"step {int(holder['at_step'])}"
    if holder.get("at_seconds") is not None:
        return f"{float(holder['at_seconds']):g}s"
    return "last frame"


def _holders(spec: dict) -> Iterator[tuple[str, dict]]:
    """``(path, holder)`` of every picture of *spec* that may name a recording's frame."""
    for index, scene in enumerate(spec.get("scenes") or []):
        if not isinstance(scene, dict):
            continue
        if scene.get("type") == "image":
            yield f"scenes.{index}", scene
        elif scene.get("type") == "statement" and isinstance(scene.get("backdrop"), dict):
            yield f"scenes.{index}.backdrop", scene["backdrop"]


def _kept(spec: dict | None) -> dict[str, str]:
    """``frame key -> file id`` of the frames *spec* (a stored revision) kept."""
    kept: dict[str, str] = {}
    for _path, holder in _holders(spec or {}):
        key = frame_key(holder)
        if key and holder.get("file_id") and holder.get("frame_of") == key:
            kept[key] = str(holder["file_id"])
    return kept


async def resolve(
    db: Any, user: Any, spec: dict, *, previous: dict | None = None, conversation_id: object = None
) -> tuple[dict, list[str]]:
    """*spec* with every recording frame it names kept as a picture, and one note per frame taken; a frame that
    cannot be taken (a recording *user* cannot read, a step it skipped) is :class:`VideoSpecError`."""
    from app.services.video_studio import assets, clips

    resolved = copy.deepcopy(spec)
    known = _kept(previous)
    notes: list[str] = []
    problems: list[SpecProblem] = []
    for path, holder in _holders(resolved):
        key = frame_key(holder)
        if key is None:
            continue
        if holder.get("file_id") and holder.get("frame_of") == key:
            known.setdefault(key, str(holder["file_id"]))
            continue
        if key in known:
            holder["file_id"], holder["frame_of"] = known[key], key
            continue
        try:
            source = await clips.resolve_frame(
                db, user, recording_id=holder["take_id"], at_step=holder.get("at_step"), at_seconds=holder.get("at_seconds")
            )
            with tempfile.TemporaryDirectory(prefix="xtm-take-frame-") as work:
                png = await clips.frame_picture(source, work)
        except clips.ClipError as exc:
            problems.append(SpecProblem(f"{path}.take_id", f"{exc} A picture of a video names {PICTURE_SOURCES}."))
            continue
        label = clips.frame_label(source)
        stem = (source.get("name") or "recording").rsplit(".", 1)[0][:60]
        row = await assets.store_bytes(
            png,
            filename=f"{stem} - {_moment(holder)}.png",
            user_id=user.id,
            source=assets.SOURCE_TAKE,
            content_type="image/png",
            conversation_id=conversation_id,
            description=f"{label[:1].upper()}{label[1:]}, kept as the picture of {path}.",
        )
        holder["file_id"], holder["frame_of"] = str(row.id), key
        known[key] = str(row.id)
        notes.append(f"{path}: {label} kept as the picture `{row.id}` (taken once; the next saves keep it).")
    if problems:
        raise VideoSpecError(problems)
    return resolved, notes
