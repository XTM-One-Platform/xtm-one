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

"""A real picture inside a picture: ``compose_picture``.

To show the real product on the screen drawn in a still - a booth screen, a
monitor, a phone - or a real wordmark on a backwall, a base picture gets 1 to
:data:`MAX_LAYERS` layers, each a picture placed either flat (``x``, ``y``,
``width`` and optionally ``height``, in the base's pixels) or in perspective
(``quad``: its four corners in the base's pixels - top left, top right, bottom
right, bottom left - the screen's own corners), with an ``opacity`` and a
``fit`` (``contain`` keeps the whole layer, ``cover`` fills the place and cuts
the layer's edges, ``stretch`` fills it as it is).

Drawn with Pillow alone - an affine resize or a perspective transform per
layer, alpha-composited in order - never in a browser. The pictures are files
the person can read (a conversation file, an upload, a template asset; an SVG
drawn by the platform's bounded rasteriser); the result is a PNG.
"""

from __future__ import annotations

import io
import math
from dataclasses import dataclass
from typing import Any

#: The most layers one picture takes.
MAX_LAYERS = 4
#: The largest base picture composed (either side, in pixels).
MAX_SIDE = 4096
#: The largest picture file read.
MAX_PICTURE_BYTES = 40 * 1024 * 1024
#: The box an SVG is drawn into before it is placed.
_SVG_BOX = 2048
FITS = ("contain", "cover", "stretch")


class PictureError(ValueError):
    """A composition the platform refuses; ``str()`` says which field and why."""


@dataclass(frozen=True)
class LayerSpec:
    """One layer: its picture and where it goes - a flat box, or a quad in perspective."""

    file_id: str
    x: float | None = None
    y: float | None = None
    width: float | None = None
    height: float | None = None
    quad: tuple[tuple[float, float], ...] | None = None
    opacity: float = 1.0
    fit: str = "contain"


def _number(value: Any, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise PictureError(f"{label} must be a number.") from None
    if not math.isfinite(number):
        raise PictureError(f"{label} must be a number.")
    return number


def parse_layers(raw: Any) -> list[LayerSpec]:
    """The layers a call asks for, checked field by field, or :class:`PictureError`."""
    from app.services.builtin_tools._shared import _file_handle_key

    if not isinstance(raw, list) or not raw:
        raise PictureError(f"layers is a list of 1 to {MAX_LAYERS} layers.")
    if len(raw) > MAX_LAYERS:
        raise PictureError(f"A picture takes at most {MAX_LAYERS} layers.")
    layers: list[LayerSpec] = []
    for index, item in enumerate(raw):
        where = f"layers.{index}"
        if not isinstance(item, dict) or not str(item.get("file_id") or "").strip():
            raise PictureError(f"{where}: a layer names its picture (file_id).")
        fit = str(item.get("fit") or "contain")
        if fit not in FITS:
            raise PictureError(f"{where}.fit is one of {', '.join(FITS)}.")
        opacity = _number(item.get("opacity", 1.0), f"{where}.opacity")
        if not 0.0 < opacity <= 1.0:
            raise PictureError(f"{where}.opacity is above 0 and at most 1.")
        quad = item.get("quad")
        if quad is not None:
            if not isinstance(quad, list) or len(quad) != 4 or not all(isinstance(p, list | tuple) and len(p) == 2 for p in quad):
                raise PictureError(
                    f"{where}.quad is four corners [x, y] in the base's pixels: top left, top right, bottom right, bottom left."
                )
            points = tuple((_number(p[0], f"{where}.quad"), _number(p[1], f"{where}.quad")) for p in quad)
            if abs(_area(points)) < 4:
                raise PictureError(f"{where}.quad encloses no area: give its four corners in order.")
            layers.append(LayerSpec(_file_handle_key(str(item["file_id"])), quad=points, opacity=opacity, fit=fit))
            continue
        if item.get("width") is None or item.get("x") is None or item.get("y") is None:
            raise PictureError(f"{where}: a layer is placed by x, y and width (its box in the base's pixels), or by quad.")
        width = _number(item["width"], f"{where}.width")
        height = None if item.get("height") is None else _number(item["height"], f"{where}.height")
        if width <= 0 or (height is not None and height <= 0):
            raise PictureError(f"{where}: width and height are above 0.")
        layers.append(
            LayerSpec(
                _file_handle_key(str(item["file_id"])),
                x=_number(item["x"], f"{where}.x"),
                y=_number(item["y"], f"{where}.y"),
                width=width,
                height=height,
                opacity=opacity,
                fit=fit,
            )
        )
    return layers


def _area(points: tuple[tuple[float, float], ...]) -> float:
    """The signed area of a polygon (the shoelace formula)."""
    return 0.5 * sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(points, points[1:] + points[:1], strict=True))


def perspective_coefficients(quad: tuple[tuple[float, float], ...], width: int, height: int) -> list[float]:
    """Pillow's ``PERSPECTIVE`` coefficients drawing a *width* x *height* picture into *quad*.

    Pillow maps each output pixel back to the picture: ``x' = (a x + b y + c)
    / (g x + h y + 1)``, ``y' = (d x + e y + f) / (g x + h y + 1)``. The eight
    unknowns are solved from the quad's corners landing on the picture's
    (top left, top right, bottom right, bottom left).
    """
    import numpy as np

    source = ((0.0, 0.0), (float(width), 0.0), (float(width), float(height)), (0.0, float(height)))
    rows, values = [], []
    for (x, y), (u, v) in zip(quad, source, strict=True):
        rows.append([x, y, 1, 0, 0, 0, -u * x, -u * y])
        values.append(u)
        rows.append([0, 0, 0, x, y, 1, -v * x, -v * y])
        values.append(v)
    try:
        solved = np.linalg.solve(np.array(rows, dtype=float), np.array(values, dtype=float))
    except np.linalg.LinAlgError:
        raise PictureError("A quad's corners are in a line: give the four corners of the place, in order.") from None
    return [float(c) for c in solved]


def fitted(picture: Any, box_w: float, box_h: float, fit: str, *, pad: tuple[int, int, int, int] = (0, 0, 0, 0)) -> Any:
    """*picture* shaped to a box of *box_w* x *box_h*'s proportions: whole and padded with *pad*, cut, or as it is."""
    from PIL import Image

    if fit == "stretch" or box_w <= 0 or box_h <= 0:
        return picture
    width, height = picture.size
    target = box_w / box_h
    if abs(width / height - target) < 1e-3:
        return picture
    if fit == "cover":
        if width / height > target:
            cut = round(height * target)
            left = (width - cut) // 2
            return picture.crop((left, 0, left + cut, height))
        cut = round(width / target)
        top = (height - cut) // 2
        return picture.crop((0, top, width, top + cut))
    canvas_w, canvas_h = (width, round(width / target)) if width / height > target else (round(height * target), height)
    canvas = Image.new("RGBA", (max(1, canvas_w), max(1, canvas_h)), pad)
    canvas.paste(picture, ((canvas.width - width) // 2, (canvas.height - height) // 2))
    return canvas


#: A row or a column of a picture is flat when its channels spread less than this many grey levels ...
CONTENT_FLAT_SPREAD = 4.0
#: ... and a flat margin's colour stays within this many levels of its edge's: the UI's own background, trimmed.
CONTENT_MARGIN_LEVELS = 6.0
#: A margin is never trimmed past this share of the picture's side: a near-empty picture keeps its content box.
CONTENT_MAX_TRIM = 0.4
#: The content box is measured on a copy at most this long.
CONTENT_SIDE = 512


def content_box(picture: Any) -> tuple[int, int, int, int] | None:
    """The box of *picture*'s content, in its pixels: the picture less the flat margins of its own background.

    A row or a column is a margin when it is flat (:data:`CONTENT_FLAT_SPREAD`)
    and the colour of the edge it continues (:data:`CONTENT_MARGIN_LEVELS`);
    the margins are trimmed from each side up to :data:`CONTENT_MAX_TRIM` of
    it. Only opaque pixels count: a transparent margin is no content either.
    A picture flat all over, or without an opaque pixel, has no content and no
    box: ``None`` - a cut takes nothing off it (:func:`cover_cuts`), and it is
    fitted whole. A picture too small to read is its own box.
    """
    import numpy as np

    source = picture.convert("RGBA")
    scale = min(1.0, CONTENT_SIDE / max(source.size))
    small = source.resize((max(1, round(source.width * scale)), max(1, round(source.height * scale)))) if scale < 1 else source
    pixels = np.asarray(small, dtype=np.float32)
    rgb, opaque = pixels[..., :3], pixels[..., 3] >= 128
    height, width = opaque.shape
    if not opaque.any() or rgb[opaque].std(axis=0).max() < CONTENT_FLAT_SPREAD:
        return None
    if height < 4 or width < 4:
        return 0, 0, source.width, source.height

    def margin(lines_rgb: Any, lines_opaque: Any, limit: int) -> int:
        """How many *lines* from the edge are flat margins of the edge's colour (each line ``N x 3`` with its mask)."""
        edge = None
        count = 0
        for line, mask in zip(lines_rgb, lines_opaque, strict=True):
            if count >= limit:
                break
            if not mask.any():
                count += 1
                continue
            shown = line[mask]
            if shown.std(axis=0).max() >= CONTENT_FLAT_SPREAD:
                break
            colour = shown.mean(axis=0)
            if edge is None:
                edge = colour
            elif np.abs(colour - edge).max() > CONTENT_MARGIN_LEVELS:
                break
            count += 1
        return count

    top = margin(rgb, opaque, int(height * CONTENT_MAX_TRIM))
    bottom = margin(rgb[::-1], opaque[::-1], int(height * CONTENT_MAX_TRIM))
    left = margin(rgb.transpose(1, 0, 2), opaque.T, int(width * CONTENT_MAX_TRIM))
    right = margin(rgb.transpose(1, 0, 2)[::-1], opaque.T[::-1], int(width * CONTENT_MAX_TRIM))
    if top + bottom >= height or left + right >= width:
        return 0, 0, source.width, source.height
    x0, y0 = math.floor(left / scale), math.floor(top / scale)
    x1, y1 = math.ceil((width - right) / scale), math.ceil((height - bottom) / scale)
    return max(0, x0), max(0, y0), min(source.width, max(x1, x0 + 1)), min(source.height, max(y1, y0 + 1))


def edge_colours(picture: Any) -> dict[str, tuple[int, int, int]]:
    """The colour each side of *picture* shows at its edge (``top``, ``bottom``, ``left``, ``right``): the UI's own background.

    The median of the opaque pixels of the two outermost rows or columns;
    a side with no opaque pixel there is black.
    """
    import numpy as np

    pixels = np.asarray(picture.convert("RGBA"), dtype=np.float32)
    sides = {
        "top": pixels[:2].reshape(-1, 4),
        "bottom": pixels[-2:].reshape(-1, 4),
        "left": pixels[:, :2].reshape(-1, 4),
        "right": pixels[:, -2:].reshape(-1, 4),
    }
    found: dict[str, tuple[int, int, int]] = {}
    for side, strip in sides.items():
        shown = strip[strip[:, 3] >= 128][:, :3]
        median = np.median(shown, axis=0) if len(shown) else np.zeros(3)
        found[side] = (int(median[0]), int(median[1]), int(median[2]))
    return found


def letterboxed(picture: Any, box_w: float, box_h: float) -> Any:
    """*picture* kept whole in a box of *box_w* x *box_h*'s proportions, its bands in the picture's own edge colours.

    A product screenshot fitted to a screen of another shape gets bands above
    and below it, or beside it, each the colour of the edge it continues - the
    UI's own background - never a black card and never a cut sidebar.
    """
    from PIL import Image, ImageDraw

    source = picture.convert("RGBA")
    width, height = source.size
    if box_w <= 0 or box_h <= 0 or abs(width / height - box_w / box_h) < 1e-3:
        return source
    colours = edge_colours(source)
    if width / height > box_w / box_h:
        canvas = Image.new("RGBA", (width, max(1, round(width * box_h / box_w))), (*colours["top"], 255))
        top = (canvas.height - height) // 2
        ImageDraw.Draw(canvas).rectangle((0, top + height, width, canvas.height), fill=(*colours["bottom"], 255))
        canvas.paste(source, (0, top), source)
        return canvas
    canvas = Image.new("RGBA", (max(1, round(height * box_w / box_h)), height), (*colours["left"], 255))
    left = (canvas.width - width) // 2
    ImageDraw.Draw(canvas).rectangle((left + width, 0, canvas.width, height), fill=(*colours["right"], 255))
    canvas.paste(source, (left, 0), source)
    return canvas


def cover_cuts(size: tuple[int, int], content: tuple[int, int, int, int] | None, box_w: float, box_h: float) -> float:
    """The share of the *content* box (pixels of a picture of *size*) a ``cover`` fit to *box_w* x *box_h* cuts away.

    0 when the crop takes margins only - a cover fit is then as good as a
    contain one - or there is no content to take (*content* ``None``: a flat
    picture), 1 when it would cut the content whole.
    """
    width, height = size
    if content is None or box_w <= 0 or box_h <= 0 or width <= 0 or height <= 0:
        return 0.0
    target = box_w / box_h
    if width / height > target:
        cut = height * target
        left, top, right, bottom = (width - cut) / 2, 0.0, (width + cut) / 2, float(height)
    else:
        cut = width / target
        left, top, right, bottom = 0.0, (height - cut) / 2, float(width), (height + cut) / 2
    x0, y0, x1, y1 = content
    area = max(1.0, float((x1 - x0) * (y1 - y0)))
    kept = max(0.0, min(x1, right) - max(x0, left)) * max(0.0, min(y1, bottom) - max(y0, top))
    return round(max(0.0, 1.0 - kept / area), 4)


def _with_opacity(picture: Any, opacity: float) -> Any:
    if opacity >= 1.0:
        return picture
    alpha = picture.getchannel("A").point(lambda a: round(a * opacity))
    faded = picture.copy()
    faded.putalpha(alpha)
    return faded


def quad_size(quad: tuple[tuple[float, float], ...]) -> tuple[float, float]:
    """The width and height a quad stands for: the mean of its opposite edges."""
    tl, tr, br, bl = quad
    width = (math.dist(tl, tr) + math.dist(bl, br)) / 2
    height = (math.dist(tl, bl) + math.dist(tr, br)) / 2
    return width, height


def compose(base: Any, layers: list[tuple[Any, LayerSpec]]) -> Any:
    """*base* with every layer drawn on it in order (Pillow images, RGBA); the composed picture."""
    from PIL import Image

    result = base.convert("RGBA")
    for picture, spec in layers:
        picture = picture.convert("RGBA")
        if spec.quad is not None:
            quad_w, quad_h = quad_size(spec.quad)
            shaped = fitted(picture, quad_w, quad_h, spec.fit)
            coefficients = perspective_coefficients(spec.quad, shaped.width, shaped.height)
            # Every base pixel outside the quad maps outside the picture: transparent.
            drawn = shaped.transform(
                result.size, Image.Transform.PERSPECTIVE, coefficients, resample=Image.Resampling.BICUBIC
            )
        else:
            width = float(spec.width or picture.width)
            height = spec.height if spec.height is not None else width * picture.height / picture.width
            shaped = fitted(picture, width, height, spec.fit)
            resized = shaped.resize((max(1, round(width)), max(1, round(height))), Image.Resampling.LANCZOS)
            drawn = Image.new("RGBA", result.size, (0, 0, 0, 0))
            drawn.paste(resized, (round(spec.x or 0), round(spec.y or 0)))
        result = Image.alpha_composite(result, _with_opacity(drawn, spec.opacity))
    return result


async def read_picture(db: Any, user: Any, file_id: str, *, what: str) -> Any:
    """A picture file *user* may read, decoded (an SVG drawn first), or :class:`PictureError`."""
    import asyncio

    from PIL import Image, UnidentifiedImageError

    from app.core.config import settings
    from app.core.utils import safe_uuid
    from app.models.file_attachment import FileAttachment
    from app.services.file_access import can_read_file
    from app.services.file_storage import FileNotFoundInStorageError, file_storage
    from app.services.svg_raster import rasterize_svg_async

    uid = safe_uuid(str(file_id))
    row = await db.get(FileAttachment, uid) if uid else None
    if row is None or not (row.content_type or "").startswith("image/") or not await can_read_file(row, user, db):
        raise PictureError(f"No picture `{file_id}` you can read for {what}.")
    if (row.size or 0) > MAX_PICTURE_BYTES:
        raise PictureError(f"The picture `{row.filename}` ({what}) is too large to compose.")
    try:
        data = await file_storage.download(row.s3_object_name)
    except FileNotFoundInStorageError:
        raise PictureError(f"The picture `{row.filename}` ({what}) is gone from storage.") from None
    if (row.content_type or "").startswith("image/svg") or data.lstrip()[:5] in (b"<svg ", b"<?xml"):
        png = await rasterize_svg_async(data, box=_SVG_BOX, strict=settings.svg_strict_rendering)
        if not png:
            raise PictureError(f"The SVG `{row.filename}` ({what}) could not be drawn.")
        data = png

    def decode() -> Any:
        with Image.open(io.BytesIO(data)) as opened:
            opened.seek(0)
            opened.load()
            return opened.convert("RGBA")

    try:
        return await asyncio.to_thread(decode)
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError) as exc:
        raise PictureError(f"`{row.filename}` ({what}) could not be read as a picture ({type(exc).__name__}).") from None


async def compose_files(db: Any, user: Any, base_file_id: str, raw_layers: Any) -> tuple[bytes, tuple[int, int]]:
    """The PNG of *base_file_id* with *raw_layers* drawn on it, and its size; :class:`PictureError` when refused."""
    import asyncio

    from app.services.builtin_tools._shared import _file_handle_key

    layers = parse_layers(raw_layers)
    base = await read_picture(db, user, _file_handle_key(str(base_file_id or "")), what="the base")
    if max(base.size) > MAX_SIDE:
        raise PictureError(f"The base picture is {base.width}x{base.height}: at most {MAX_SIDE} px a side.")
    pictures = [
        (await read_picture(db, user, spec.file_id, what=f"layer {index}"), spec) for index, spec in enumerate(layers)
    ]

    def draw() -> bytes:
        composed = compose(base, pictures)
        out = io.BytesIO()
        composed.save(out, format="PNG", optimize=True)
        return out.getvalue()

    return await asyncio.to_thread(draw), base.size
