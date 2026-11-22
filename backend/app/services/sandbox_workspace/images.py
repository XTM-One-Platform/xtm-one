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

"""Convert, resize and compress workspace images: the plan and the conversion, pure.

``sandbox_convert_images`` (``builtin_tools/sandbox_workspace.py``) turns a list
of ``{from, to, max_width, max_height, quality}`` conversions into one
workspace commit: a PNG capture becomes a WebP for a README, a screenshot is
bounded to a width, a JPEG is recompressed. Before it, an agent wrote such an
image through a ``code_interpreter`` Pillow script (gap image-convert-tool).

The output format is the suffix of ``to`` (``.png``, ``.jpg`` / ``.jpeg``,
``.webp``); the source is any image Pillow opens. A resize keeps the aspect
ratio and never enlarges. ``quality`` (1 to 100) applies to JPEG and lossy
WebP; ``lossless`` writes a lossless WebP. Animated sources are refused: one
frame only would be kept. Nothing here touches the workspace: the tool reads
the source bytes, calls :func:`convert` and commits the results together.
"""

from __future__ import annotations

import io
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from app.services.sandbox_workspace.paths import display, normalize

#: Conversions one call takes at most, and the largest source read.
MAX_CONVERSIONS = 50
MAX_SOURCE_BYTES = 25 * 1024 * 1024
#: The largest decoded image (width x height): above it a source is refused,
#: never decompressed (a crafted PNG can expand to gigabytes).
MAX_PIXELS = 50_000_000
DEFAULT_QUALITY = 82

#: Output format of each target suffix, as Pillow names it.
FORMATS = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".webp": "WEBP"}


class ImageError(ValueError):
    """A conversion refused; ``str()`` says which and why, for the model."""


@dataclass(frozen=True)
class Conversion:
    source: str
    target: str
    format: str
    max_width: int | None = None
    max_height: int | None = None
    quality: int = DEFAULT_QUALITY
    lossless: bool = False


@dataclass(frozen=True)
class Converted:
    conversion: Conversion
    data: bytes
    before_size: tuple[int, int]
    after_size: tuple[int, int]
    before_bytes: int

    def line(self) -> str:
        c = self.conversion
        (w0, h0), (w1, h1) = self.before_size, self.after_size
        resized = f", {w0}x{h0} -> {w1}x{h1}" if (w0, h0) != (w1, h1) else f", {w1}x{h1}"
        return (
            f"{display(c.source)} -> {display(c.target)} ({c.format}{resized}, "
            f"{_kb(self.before_bytes)} -> {_kb(len(self.data))})"
        )


def _kb(n: int) -> str:
    return f"{n / 1024:.1f} KB"


def _bound(value: object, name: str, low: int, high: int) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ImageError(f"{name} is a whole number, not {value!r}.")
    if not isinstance(value, (int, str, float)):
        raise ImageError(f"{name} is a whole number, not {value!r}.")
    try:
        number: int = int(value)
    except (TypeError, ValueError):
        raise ImageError(f"{name} is a whole number, not {value!r}.") from None
    if not low <= number <= high:
        raise ImageError(f"{name} must be between {low} and {high} (got {number}).")
    return number


def parse(conversions: Iterable[Mapping[str, object]] | Mapping[str, object] | None) -> list[Conversion]:
    """The conversions of a call, checked; refuses the whole call on the first bad one."""
    items = [conversions] if isinstance(conversions, Mapping) else list(conversions or [])
    if not items:
        raise ImageError("give at least one conversion {from, to}.")
    if len(items) > MAX_CONVERSIONS:
        raise ImageError(f"{len(items)} conversions: at most {MAX_CONVERSIONS} per call.")
    parsed: list[Conversion] = []
    targets: set[str] = set()
    for item in items:
        if not isinstance(item, Mapping) or not item.get("from") or not item.get("to"):
            raise ImageError("each conversion needs both 'from' and 'to' (workspace paths).")
        source, target = normalize(str(item["from"])), normalize(str(item["to"]))
        suffix = "." + target.rsplit(".", 1)[-1].lower() if "." in target.rsplit("/", 1)[-1] else ""
        fmt = FORMATS.get(suffix)
        if fmt is None:
            raise ImageError(
                f"{display(target)}: the output format is the suffix of 'to', one of {', '.join(sorted(FORMATS))}."
            )
        if target in targets:
            raise ImageError(f"two conversions write {display(target)}.")
        targets.add(target)
        lossless = bool(item.get("lossless"))
        if lossless and fmt != "WEBP":
            raise ImageError(f"{display(target)}: lossless applies to a .webp target only (PNG is always lossless).")
        quality = _bound(item.get("quality"), "quality", 1, 100)
        parsed.append(
            Conversion(
                source=source,
                target=target,
                format=fmt,
                max_width=_bound(item.get("max_width"), "max_width", 1, 20_000),
                max_height=_bound(item.get("max_height"), "max_height", 1, 20_000),
                quality=DEFAULT_QUALITY if quality is None else quality,
                lossless=lossless,
            )
        )
    return parsed


def _fitted(size: tuple[int, int], max_width: int | None, max_height: int | None) -> tuple[int, int]:
    width, height = size
    scale = 1.0
    if max_width and width > max_width:
        scale = min(scale, max_width / width)
    if max_height and height > max_height:
        scale = min(scale, max_height / height)
    if scale >= 1.0:
        return size
    return max(1, round(width * scale)), max(1, round(height * scale))


def convert(conversion: Conversion, data: bytes) -> Converted:
    """Convert one source image; :class:`ImageError` when it cannot be read or written."""
    from PIL import Image, UnidentifiedImageError

    if len(data) > MAX_SOURCE_BYTES:
        raise ImageError(f"{display(conversion.source)} is over {MAX_SOURCE_BYTES // (1024 * 1024)} MB.")
    try:
        with Image.open(io.BytesIO(data)) as probe:
            width, height = probe.size
            if width * height > MAX_PIXELS:
                raise ImageError(
                    f"{display(conversion.source)} is {width}x{height}: over {MAX_PIXELS:,} pixels, not decoded."
                )
            if getattr(probe, "n_frames", 1) > 1:
                raise ImageError(f"{display(conversion.source)} is animated: only still images are converted.")
            probe.load()
            image = probe.copy()
    except ImageError:
        raise
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ImageError(f"{display(conversion.source)} is not an image Pillow reads ({exc}).") from None
    has_alpha = image.mode in ("RGBA", "LA", "PA") or (image.mode == "P" and "transparency" in image.info)
    target_size = _fitted(image.size, conversion.max_width, conversion.max_height)
    if target_size != image.size:
        if image.mode in ("P", "PA", "1"):
            # Pillow resizes palette and bilevel images with nearest-neighbour whatever
            # the filter asked: expand them first so LANCZOS applies.
            image = image.convert("RGBA" if has_alpha else ("L" if image.mode == "1" else "RGB"))
        image = image.resize(target_size, Image.Resampling.LANCZOS)
    options: dict[str, object] = {}
    if conversion.format == "JPEG":
        if image.mode != "RGB":
            # JPEG has no alpha: flatten on white rather than on black.
            background = Image.new("RGB", image.size, (255, 255, 255))
            rgba = image.convert("RGBA")
            background.paste(rgba, mask=rgba.getchannel("A"))
            image = background
        options = {"quality": conversion.quality, "optimize": True}
    elif conversion.format == "WEBP":
        image = image.convert("RGBA" if has_alpha else "RGB")
        options = {"lossless": True} if conversion.lossless else {"quality": conversion.quality, "method": 6}
    else:
        if image.mode not in ("RGB", "RGBA", "L", "LA", "P"):
            image = image.convert("RGBA" if has_alpha else "RGB")
        options = {"optimize": True}
    out = io.BytesIO()
    image.save(out, format=conversion.format, **options)
    return Converted(
        conversion=conversion,
        data=out.getvalue(),
        before_size=(width, height),
        after_size=image.size,
        before_bytes=len(data),
    )
