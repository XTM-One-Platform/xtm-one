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

"""Regions of a start picture an AI shot keeps exactly as they are.

A video model asked to bring a photo to life redraws everything in it, its
words included: a team photo holding a banner comes out with the banner's
letters melted, and the text check fails the shot - rightly, every time. A
shot asked with ``keep_regions`` (``animate.keep`` on an image scene: boxes
``[x, y, width, height]`` in shares of the picture) keeps those parts of the
still instead:

1. the shot's camera is measured against the still (``motion.align``), the
   regions left out of the measure - the model redraws what is in them -,
   then smoothed over time (``motion.smooth``);
2. in every frame, the still's pixels of each region are drawn back where the
   camera has taken them, with a soft edge, brought to the frame's exposure
   (a gain and an offset per channel, fitted on the band around the regions,
   where the room is both the still's and the shot's).

The words there are then the still's own, and the text check reads the
result. A shot whose camera cannot be followed - the still not found in its
first frame, in too few frames, or lost too long - fails as "kept region
lost" (:class:`RegionLostError`), its video kept on the row like a drifted
shot. What crosses a region (a hand over the banner) is drawn over by the
still: a region is for what nothing in the shot moves across.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from app.services.video_studio.motion import Similarity

#: The params key of a shot's kept regions: ``{"boxes": [[x, y, w, h], ...], "drawn_file_id": ...}``.
KEY = "keep_regions"
#: The soft edge a region is drawn with, and the band read for the frame's exposure, as shares of the short side.
FEATHER_SHARE = 0.004
RING_SHARE = 0.025
#: The share of frames the still must be found in, and the longest run (seconds) it may be lost for.
MIN_FOUND_SHARE = 0.85
MAX_LOST_SECONDS = 0.5
#: How far a frame's exposure may be from the still's before the match is held there.
_GAIN = (0.75, 1.35)
_OFFSET = 40.0


class RegionLostError(RuntimeError):
    """The shot's camera could not be followed from the still: ``str()`` is the shot's failure."""


@dataclass
class Kept:
    """What keeping the regions did, for the shot's notes."""

    frames: int
    found: int
    notes: list[str] = field(default_factory=list)


def boxes_of(params: dict | None) -> list[list[float]]:
    """The regions a shot keeps (``[]`` for none)."""
    kept = (params or {}).get(KEY)
    boxes = kept.get("boxes") if isinstance(kept, dict) else None
    return [list(map(float, box)) for box in boxes or [] if isinstance(box, list) and len(box) == 4]


def same_boxes(a: Any, b: Any) -> bool:
    """Whether two lists of regions are the same, to the stored precision."""
    from app.services.video_studio.spec import kept_boxes

    try:
        return kept_boxes(list(a or [])) == kept_boxes(list(b or []))
    except ValueError:
        return False


def frame_rects(picture_size: tuple[int, int], frame_size: tuple[int, int], boxes: list[list[float]]) -> list[tuple]:
    """Each region in the pixels of a frame of *frame_size* made from a picture of *picture_size*.

    The picture is cut around its centre to the frame's shape, as a provider
    fits a start image (``frame_qc.cut_box``), then scaled; a region cut off
    by that is kept in part, one entirely outside is dropped.
    """
    from app.services.video_studio.frame_qc import cut_box

    width, height = picture_size
    fw, fh = frame_size
    left, top, right, bottom = cut_box(picture_size, fw / fh)
    sx, sy = fw / max(right - left, 1), fh / max(bottom - top, 1)
    rects = []
    for x, y, w, h in boxes:
        x0, y0 = (x * width - left) * sx, (y * height - top) * sy
        x1, y1 = ((x + w) * width - left) * sx, ((y + h) * height - top) * sy
        x0, y0, x1, y1 = max(0.0, x0), max(0.0, y0), min(float(fw), x1), min(float(fh), y1)
        if x1 - x0 >= 2 and y1 - y0 >= 2:
            rects.append((x0, y0, x1, y1))
    return rects


def framed_still(picture: Any, frame_size: tuple[int, int]) -> Any:
    """*picture* (Pillow) cut to the frame's shape around its centre and scaled to it, RGB."""
    from PIL import Image

    from app.services.video_studio.frame_qc import cut_box

    box = cut_box(picture.size, frame_size[0] / frame_size[1])
    return picture.convert("RGB").crop(box).resize(frame_size, Image.Resampling.LANCZOS)


def _rect_mask(size: tuple[int, int], rects: list[tuple], grow: float, blur: float) -> Any:
    """``L`` mask of *rects* grown by *grow* pixels, its edge blurred by *blur*."""
    from PIL import Image, ImageDraw, ImageFilter

    mask = Image.new("L", size, 0)
    draw = ImageDraw.Draw(mask)
    for x0, y0, x1, y1 in rects:
        draw.rectangle((x0 - grow, y0 - grow, x1 + grow, y1 + grow), fill=255)
    return mask.filter(ImageFilter.GaussianBlur(blur)) if blur > 0 else mask


def judge(found: list[bool], fps: float) -> None:
    """Refuse a shot whose camera was not followed from the still: not in its first frame, too often, too long."""
    from app.services.video_studio.motion import longest_run

    total = len(found)
    if not total:
        raise RegionLostError("kept region lost: the shot has no frame to keep the regions in.")
    seen = sum(found)
    allowed = max(3, round(MAX_LOST_SECONDS * (fps or 24)))
    if not found[0] or seen < MIN_FOUND_SHARE * total or longest_run(found) > allowed:
        opening = ", not in its first: the shot does not open on its start picture" if not found[0] else ""
        raise RegionLostError(
            f"kept region lost: the camera could not be followed from the still (found in {seen} of {total} "
            f"frames{opening}). Generate it again with a calmer camera move and the regions in view, or keep "
            "smaller regions."
        )


def _exposure(still: Any, frame: Any, ring: Any) -> tuple[Any, Any]:
    """Per channel gain and offset bringing *still*'s pixels to *frame*'s, fitted where *ring* is set."""
    import numpy as np

    chosen = ring > 0.5
    if chosen.sum() < 64:
        return np.ones(3, dtype=np.float32), np.zeros(3, dtype=np.float32)
    s, f = still[chosen], frame[chosen]
    mean_s, mean_f = s.mean(axis=0), f.mean(axis=0)
    spread = ((s - mean_s) ** 2).mean(axis=0)
    gain = np.where(spread > 4.0, ((s - mean_s) * (f - mean_f)).mean(axis=0) / np.maximum(spread, 1e-6), 1.0)
    gain = np.clip(gain, *_GAIN)
    offset = np.clip(mean_f - gain * mean_s, -_OFFSET, _OFFSET)
    return gain.astype(np.float32), offset.astype(np.float32)


def draw(frame: Any, still: Any, mask: Any, ring: Any, rects: list[tuple], motion: Similarity, pad: float) -> Any:
    """*frame* (``H x W x 3`` uint8) with *still*'s regions drawn where *motion* (still to frame, pixels) takes them.

    *mask* and *ring* are the still's ``L`` masks of the regions and of the
    band read for the exposure. Only the box the moved regions cover is warped.
    """
    import numpy as np
    from PIL import Image

    height, width = frame.shape[:2]
    corners = np.array([(x, y) for x0, y0, x1, y1 in rects for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))])
    moved = motion.apply(corners)
    low, high = moved.min(axis=0) - pad, moved.max(axis=0) + pad
    x0, y0 = max(0, int(np.floor(low[0]))), max(0, int(np.floor(low[1])))
    x1, y1 = min(width, int(np.ceil(high[0]))), min(height, int(np.ceil(high[1])))
    if x1 - x0 < 2 or y1 - y0 < 2:
        return frame
    local = motion.then(Similarity(1.0, 0.0, -x0, -y0)).coefficients()
    size = (x1 - x0, y1 - y0)
    warped = np.asarray(still.transform(size, Image.Transform.AFFINE, local, resample=Image.Resampling.BICUBIC), dtype=np.float32)
    alpha = np.asarray(mask.transform(size, Image.Transform.AFFINE, local, resample=Image.Resampling.BILINEAR), dtype=np.float32) / 255.0
    band = np.asarray(ring.transform(size, Image.Transform.AFFINE, local, resample=Image.Resampling.BILINEAR), dtype=np.float32) / 255.0
    region = frame[y0:y1, x0:x1].astype(np.float32)
    gain, offset = _exposure(warped, region, band)
    matched = np.clip(warped * gain + offset, 0.0, 255.0)
    out = frame.copy()
    out[y0:y1, x0:x1] = np.clip(region * (1.0 - alpha[..., None]) + matched * alpha[..., None], 0, 255).astype(np.uint8)
    return out


def track(still_grey: Any, frames: list[Any], weight: Any) -> tuple[list[Similarity], list[bool]]:
    """The motion from the still to each frame (analysis pixels), each started from the last one found."""
    from app.services.video_studio import motion

    motions: list[Similarity] = []
    found: list[bool] = []
    previous = Similarity()
    for grey in frames:
        fit = motion.align(still_grey, grey, init=previous, weight=weight)
        motions.append(fit.motion)
        found.append(fit.found)
        if fit.found:
            previous = fit.motion
    return motions, found


async def keep(video_path: str, out_path: str, picture: Any, boxes: list[list[float]]) -> Kept:
    """Draw *picture*'s *boxes* (shares of it) back into every frame of *video_path*, following its camera, to *out_path*.

    Raises :class:`RegionLostError` when the camera cannot be followed from the still.
    """
    import numpy as np
    from PIL import Image

    from app.services.video_studio import encoder, motion

    info = await encoder.probe(video_path)
    width, height = info.width, info.height
    rects = frame_rects(picture.size, (width, height), boxes)
    if not rects:
        return Kept(0, 0, ["The regions to keep are outside the shot's frame: nothing was kept."])
    still = framed_still(picture, (width, height))
    short = min(width, height)
    feather = max(1.0, FEATHER_SHARE * short)
    mask = _rect_mask((width, height), rects, feather, feather)
    inner = np.asarray(_rect_mask((width, height), rects, 2 * feather, 0), dtype=np.float32)
    band = 2 * feather + RING_SHARE * short
    outer = np.asarray(_rect_mask((width, height), rects, band, 0), dtype=np.float32)
    ring = Image.fromarray(np.clip(outer - inner, 0, 255).astype(np.uint8), "L")
    still_grey, factor = motion.grey(still)
    # The regions are what the model redraws: the camera is read on the rest of the picture.
    small = Image.fromarray(inner.astype(np.uint8), "L").resize(
        (still_grey.shape[1], still_grey.shape[0]), Image.Resampling.BOX
    )
    weight = 1.0 - np.asarray(small, dtype=np.float32) / 255.0
    frames: list[Any] = []
    async for raw in encoder.iter_rgb_frames(video_path, width=width, height=height):
        frames.append(motion.grey(np.frombuffer(raw, dtype=np.uint8).reshape(height, width, 3))[0].astype(np.uint8))
    motions, found = await asyncio.to_thread(track, still_grey, frames, weight)
    del frames
    judge(found, info.fps)
    smoothed = [m.scaled(1.0 / factor) for m in motion.smooth(motions, found)]
    pad = band + 2

    def drawn(index: int, raw: bytes) -> bytes:
        frame = np.frombuffer(raw, dtype=np.uint8).reshape(height, width, 3)
        return draw(frame, still, mask, ring, rects, smoothed[min(index, len(smoothed) - 1)], pad).tobytes()

    await encoder.transform_frames(video_path, out_path, drawn, width=width, height=height, fps=info.fps or 24.0)
    seen = sum(found)
    count = f"{len(rects)} region{'s' if len(rects) > 1 else ''}"
    return Kept(
        len(found),
        seen,
        [
            f"Kept from the start picture in every frame: {count}, drawn back where the shot's camera takes them "
            f"(followed in {seen} of {len(found)} frames): their text is the picture's own."
        ],
    )
