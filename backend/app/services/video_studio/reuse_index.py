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

"""What a whole render takes instead of drawing: the reuse index its pre-flight reads before estimating.

A render serves two things without drawing them:

* **Figure frames.** A figure scene's drawing is kept beside the video by its
  key (``figure_cache``) and played as it is by the next render of the
  same key - or, drawn sharper by a render at a higher quality, rescaled
  (``figure_cache.find_sharper``) - no page opened.
* **Composed frames.** The segments a former attempt of the same render closed
  whole (``compositor.kept_segments``, ``render_segments``): an attempt resumed
  after a stall, a lost lease or a handover draws only the rest. They are the
  render's own, so a render about to be queued has none.

A figure's key is ``<content>-<scale>x<fps>`` (``figures.render_key``), its
content hashing the times its narration says each word - known once the voice
is spoken, never before a render is queued. So each whole render records, for
every figure it drew or took (``VideoRender.measured["figures"]``,
:func:`figure_entries`), its key beside the inputs that decide its content and
are known before (:func:`figure_inputs`): the scene's digest
(``render_checks.scene_digests``: what it shows, its neighbours' edges, the
transitions between them, the look, the music), the voice digests of its
narration and of its neighbours' (``voice.digest_of``: the take that times its
words, the takes whose length its transitions follow), the figure page's
version and the release. The pre-flight computes the same inputs and reads the
last whole renders of the video, at any quality (:func:`served`): a figure
whose inputs one recorded is served when a drawing of that content is kept
beside the video at the size and frame rate the render asks, or sharper
(``figure_cache.serves``) - its frames are not counted.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from app.core.utils import format_exception_message

logger = logging.getLogger(__name__)

#: Moved when what :func:`figure_inputs` hashes changes: an entry recorded under another version is never matched.
INDEX_VERSION = 1
#: The latest whole renders of a video the pre-flight reads the index from, whatever their quality.
INDEXED_RENDERS = 5


@dataclass(frozen=True)
class Served:
    """What the reuse index serves of a render about to be queued (:func:`served`)."""

    #: The figure scenes of the render.
    figures: int = 0
    #: Of them, those a drawing kept beside the video serves, for the inputs the render draws them from.
    served: int = 0
    #: Their length in the plan: what the estimate leaves out of the figure frames to draw.
    seconds: float = 0.0


def voice_digests(target: Any, spec: Any, lexicon: Any) -> dict[str, str]:
    """``scene id -> the digest of its narration as spoken`` (``voice.digest_of``); empty without a voice."""
    from app.services.video_studio import voice

    if target is None:
        return {}
    return {line.scene_id: voice.digest_of(target, line, spec.voiceover, lexicon) for line in voice.lines_of(spec)}


def figure_inputs(spec: Any, digests: Mapping[str, str], voices: Mapping[str, str]) -> dict[str, str]:
    """``scene id -> what its figure's content is drawn from, as known before a render`` for each figure of *spec*.

    *digests* are ``render_checks.scene_digests`` of the effective spec, *voices* :func:`voice_digests`. Never the
    size nor the frame rate, which are not its content: a drawing sharper than a render asks serves it. A scene
    without a digest has no inputs: it is never matched.
    """
    from app.core.config import settings
    from app.services.video_studio.figures import RENDER_VERSION

    ids = [scene.id or "" for scene in spec.scenes]
    found: dict[str, str] = {}
    for index, scene in enumerate(spec.scenes):
        if scene.type != "figure" or not scene.id or not digests.get(scene.id):
            continue
        before = voices.get(ids[index - 1], "") if index > 0 else "start"
        after = voices.get(ids[index + 1], "") if index + 1 < len(ids) else "end"
        parts = [
            f"v{INDEX_VERSION}",
            f"figure{RENDER_VERSION}",
            str(settings.app_version),
            digests[scene.id],
            f"before:{before}",
            f"voice:{voices.get(scene.id, '')}",
            f"after:{after}",
        ]
        found[scene.id] = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:40]
    return found


def figure_entries(inputs: Mapping[str, str], drawings: Mapping[str, Any]) -> list[dict]:
    """What a whole render records of each figure (``VideoRender.measured["figures"]``): the reuse index's entries.

    *drawings* are ``scene id -> figures.Drawing``: its key, the frames it drew and those a kept drawing served.
    """
    return [
        {
            "scene_id": scene_id,
            "inputs": inputs.get(scene_id),
            "key": drawing.key,
            "drawn": drawing.drawn,
            "reused": drawing.reused,
        }
        for scene_id, drawing in drawings.items()
        if drawing.key
    ]


async def _indexed(db: Any, video_id: str) -> dict[str, str]:
    """``inputs -> figure content`` the last whole renders of *video_id* recorded, the newest first."""
    from sqlalchemy import select

    from app.models.video_studio import VideoRender
    from app.services.video_studio.figures import parse_key

    rows = (
        (
            await db.execute(
                select(VideoRender.measured)
                .where(
                    VideoRender.project_id == video_id,
                    VideoRender.scene_id.is_(None),
                    VideoRender.measured.is_not(None),
                )
                .order_by(VideoRender.created_at.desc())
                .limit(INDEXED_RENDERS)
            )
        )
        .scalars()
        .all()
    )
    found: dict[str, str] = {}
    for measured in rows:
        entries = measured.get("figures") if isinstance(measured, dict) else None
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, dict) or not entry.get("inputs"):
                continue
            # A key of an older release names no content: never served.
            if (parsed := parse_key(str(entry.get("key") or ""))) is not None:
                found.setdefault(str(entry["inputs"]), parsed[0])
    return found


async def voice_target(db: Any, user: Any, spec: Any) -> Any:
    """The voice the render would speak *spec* with (``voice.resolve``), as ``render._read_references`` reads it."""
    from app.services.video_studio import voice

    if user is None or not spec.voiceover.enabled or not any(scene.narration for scene in spec.scenes):
        return None
    try:
        return await voice.resolve(db, user.id, spec.voiceover)
    except Exception as exc:  # no voice: the narrations time nothing, as in the render
        logger.debug("Voice not resolved for the reuse index: %s", format_exception_message(exc))
        return None


async def served(
    db: Any,
    effective: dict,
    *,
    video_id: str,
    user: Any,
    size: tuple[int, int],
    fps: int,
    lengths: Mapping[str, float],
) -> Served:
    """The figures of a whole render of *effective* at *size* and *fps* the cache will serve, before it is queued.

    *lengths* are ``scene id -> its length`` of each figure scene in the plan. Nothing indexed for the video reads
    nothing more; a store that cannot be listed serves nothing (every figure is counted).
    """
    from app.services.file_storage import file_storage
    from app.services.video_studio import figure_cache, figures, lexicon_store, render_checks
    from app.services.video_studio.spec import design_size, parse_video_spec

    counted = Served(figures=len(lengths))
    if not lengths:
        return counted
    indexed = await _indexed(db, video_id)
    if not indexed:
        return counted
    spec = parse_video_spec(effective)
    lexicon = await lexicon_store.for_voiceover(db, spec.voiceover)
    digests = await render_checks.scene_digests(db, effective, lexicon)
    voices = voice_digests(await voice_target(db, user, spec), spec, lexicon)
    inputs = figure_inputs(spec, digests, voices)
    wanted = {scene_id: indexed[drawn_from] for scene_id, drawn_from in inputs.items() if scene_id in lengths and drawn_from in indexed}
    if not wanted:
        return counted
    try:
        names = await file_storage.list_object_names(f"{figure_cache.PREFIX}/{video_id}/")
    except Exception as exc:
        logger.info("Figure drawings of %s not listed for a pre-flight: %s", video_id, format_exception_message(exc))
        return counted
    stored = [name.rsplit("/", 1)[-1].removesuffix(".mp4") for name in names]
    # The scale and frame rate the render draws a figure at (``render._draw_figures``).
    scale = max(1, round(size[0] / design_size(spec.format.aspect_ratio)[0]))
    scenes = [
        scene_id
        for scene_id, content in wanted.items()
        if any(figure_cache.serves(key, figures.variant_key(content, scale, fps)) for key in stored)
    ]
    return Served(figures=len(lengths), served=len(scenes), seconds=sum(float(lengths[s]) for s in scenes))
