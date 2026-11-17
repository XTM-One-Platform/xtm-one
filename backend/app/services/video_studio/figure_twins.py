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

"""The twin a figure draws a template asset as, in the video's scheme.

A deck is drawn in one scheme, and the pictures its slides place are the ones
drawn for it: a light deck's architecture places the dark-on-light product
logos. A figure scene draws its block on the video's palette, in the video's
scheme (``figures.figure_document``), so a light deck's block on a dark video
drew dark ink on a dark figure - and the only way out was to copy the block
into ``block_spec`` and swap its logos by hand, the figure no longer following
its deck.

A template asset declares its twin in its description, by the name of the
twin's file (``opencti-logo-light.svg``: "... opencti-logo.svg is the
light-on-dark twin"): :func:`named` reads which assets of the same template a
picture of the block names, or is named by. Of those, a twin draws the same
shape - its ink's box in the same proportions, within :data:`SAME_SHAPE` - in
another tone: the pictures are read (``image_geometry.image_tone``: the mean
luminance of their ink, kept by stored object in ``render_cache``), never
their words. A picture is drawn as the twin that suits the scheme better by
:data:`MIN_TONE_GAP` (:func:`pick`): on a dark figure the lighter ink, on a
light one the darker - an opaque picture is its own surface, so the other way
round. Only a twin the person the figure is drawn for may read is drawn
(``file_access.can_read_file``). The block's file ids are swapped in the
figure's slide (:func:`themed`), so its still, its render key and the pictures
its page may fetch follow; a save says which (``figures.twin_lines``). A
picture read past :data:`TONE_SECONDS` is drawn as the slide places it.

A raster a template keeps for the slides that already place it names the
vector that replaces it (``opencrq-logo-light.png``: "... place
opencrq-logo-light.svg instead"): of the assets a raster names, or is named
by, a vector drawing its shape in its tone is its successor (:func:`successor`:
the form is the stored content type, the shape and the tone its pixels, never
the words). A raster is drawn as its successor, and its twin is looked for
among the successor's: on a dark video, a light deck's
``opencrq-logo-light.png`` is drawn as ``opencrq-logo.svg``, sharp at 4K,
where it was drawn as the dark raster. A raster twin with a successor of its
own is drawn as that vector. The deck is never written to; the save names the
raster and the vector drawn for it (``figures.twin_lines``).
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import logging
import re
import uuid
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from app.core.utils import format_exception_message, safe_uuid

logger = logging.getLogger(__name__)

#: Two pictures draw the same shape when their ink's boxes are this close in proportions (a share of the ratio).
SAME_SHAPE = 0.08
#: A twin is drawn in place of a picture when it suits the scheme by this much more (luminance, 0-1).
MIN_TONE_GAP = 0.15
#: The longest the pictures of one figure are read for their tone: past it, the rest are drawn as placed.
TONE_SECONDS = 15.0
#: The most pictures of one figure looked at.
MAX_PICTURES = 48
CACHE_KIND = "figure-twin-tone"


@dataclass(frozen=True)
class Asset:
    """A template asset a figure may draw: its file, its name, its words and where its bytes are."""

    id: str
    filename: str
    description: str
    container: str
    storage_key: str
    content_type: str
    size: int

    @property
    def vector(self) -> bool:
        """Whether it is drawn from its shapes (an SVG), sharp at any size, rather than from its pixels."""
        return self.content_type.split(";", 1)[0].strip().lower() == "image/svg+xml"


@dataclass(frozen=True)
class Tone:
    """How light a picture draws (``image_geometry.image_tone``)."""

    tone: float
    opaque: bool
    ratio: float

    def suits(self, scheme: str) -> float:
        """How well it draws on a figure of *scheme* (0-1): transparent ink against the figure's surface - light ink
        on a dark one -, an opaque picture as that surface - a dark picture on a dark one."""
        light = 1.0 - self.tone if self.opaque else self.tone
        return light if scheme == "dark" else 1.0 - light

    def same_shape(self, other: Tone) -> bool:
        return self.opaque == other.opaque and abs(other.ratio / max(self.ratio, 1e-6) - 1.0) <= SAME_SHAPE

    def same_tone(self, other: Tone) -> bool:
        """Whether *other* draws in this tone: closer than :data:`MIN_TONE_GAP`, neither suiting a scheme better."""
        return self.opaque == other.opaque and abs(other.tone - self.tone) < MIN_TONE_GAP


@dataclass(frozen=True)
class Twin:
    """A picture of a figure's slide drawn as its twin: the file id as the slide spells it, the twin's, their names.

    A raster drawn as a vector (:func:`successor`) is ``sharpened``; ``through`` names the asset it went through
    when the vector is not the one it names itself: its successor's twin, or its raster twin's successor."""

    picture: str
    twin: str
    filename: str
    twin_filename: str
    sharpened: bool = False
    through: str = ""


def _names(description: str | None, filename: str | None) -> bool:
    """Whether *description* names the file *filename*, as a whole name (``logo.svg`` is not in ``xtm-logo.svg``)."""
    if not description or not filename:
        return False
    return re.search(rf"(?<![\w.-]){re.escape(filename)}(?![\w-])", description, re.IGNORECASE) is not None


def named(picture: Asset, siblings: list[Asset]) -> list[Asset]:
    """The assets of *picture*'s template its description names by file, or whose description names it."""
    return [
        other
        for other in siblings
        if other.id != picture.id
        and other.container == picture.container
        and (_names(picture.description, other.filename) or _names(other.description, picture.filename))
    ]


def pick(mine: Tone, candidates: list[tuple[Asset, Tone]], scheme: str) -> Asset | None:
    """The twin of a picture of tone *mine* drawn on a figure of *scheme*: of the *candidates* drawing the same shape,
    the one that suits the scheme best, when it does by :data:`MIN_TONE_GAP` more; ``None`` keeps the picture."""
    shaped = [(asset, tone) for asset, tone in candidates if mine.same_shape(tone)]
    if not shaped:
        return None
    asset, tone = max(shaped, key=lambda found: found[1].suits(scheme))
    return asset if tone.suits(scheme) - mine.suits(scheme) >= MIN_TONE_GAP else None


def successor(picture: Asset, mine: Tone, candidates: list[tuple[Asset, Tone]]) -> Asset | None:
    """The vector a raster *picture* of tone *mine* is drawn as: of the *candidates* (the assets it names or is named
    by), a vector drawing its shape in its tone, the first by name; ``None`` for a vector or a raster without one."""
    if picture.vector:
        return None
    found = sorted(
        (asset for asset, tone in candidates if asset.vector and mine.same_shape(tone) and mine.same_tone(tone)),
        key=lambda asset: (asset.filename.lower(), asset.id),
    )
    return found[0] if found else None


async def _template_assets(db: Any, ids: list[uuid.UUID]) -> list[Asset]:
    """The rows of *ids* that are template assets, read as :class:`Asset`."""
    from sqlalchemy import select

    from app.models.file_attachment import FileAttachment
    from app.models.template_container import TEMPLATE_ASSET_SOURCE

    rows = await db.execute(
        select(*_columns(FileAttachment)).where(
            FileAttachment.id.in_(ids),
            FileAttachment.source == TEMPLATE_ASSET_SOURCE,
            FileAttachment.template_container_id.is_not(None),
        )
    )
    return [_asset(row) for row in rows.all()]


async def _siblings(db: Any, containers: set[str]) -> list[Asset]:
    """Every picture of the templates *containers*: where a twin is looked for."""
    from sqlalchemy import select

    from app.models.file_attachment import FileAttachment
    from app.models.template_container import TEMPLATE_ASSET_SOURCE

    rows = await db.execute(
        select(*_columns(FileAttachment)).where(
            FileAttachment.template_container_id.in_([uuid.UUID(container) for container in containers]),
            FileAttachment.source == TEMPLATE_ASSET_SOURCE,
            FileAttachment.content_type.like("image/%"),
        )
    )
    return [_asset(row) for row in rows.all()]


def _columns(model: Any) -> tuple:
    """What :class:`Asset` is read from, for both reads."""
    return (
        model.id,
        model.filename,
        model.description,
        model.template_container_id,
        model.s3_object_name,
        model.content_type,
        model.size,
    )


def _asset(row: Any) -> Asset:
    file_id, filename, description, container, storage_key, content_type, size = row
    return Asset(
        str(file_id),
        str(filename or ""),
        str(description or ""),
        str(container),
        str(storage_key),
        str(content_type or ""),
        int(size or 0),
    )


async def _tone_of(asset: Asset) -> Tone | None:
    """How light *asset* draws, kept by its stored object (``render_cache``); ``None`` for a picture not read."""
    from app.services.file_storage import file_storage
    from app.services.image_geometry import IMAGE_TONE_VERSION, MAX_ANALYSIS_BYTES, image_tone
    from app.services.video_studio import render_cache

    digest = hashlib.sha256(
        json.dumps({"version": IMAGE_TONE_VERSION, "object": asset.storage_key}, sort_keys=True).encode("utf-8")
    ).hexdigest()
    kept = await render_cache.get_json(CACHE_KIND, digest)
    if isinstance(kept, dict) and "tone" in kept:
        read = kept["tone"]
    else:
        read = None
        if asset.size <= MAX_ANALYSIS_BYTES:
            read = await image_tone(await file_storage.download(asset.storage_key), asset.content_type)
        await render_cache.put_json(CACHE_KIND, digest, {"tone": read})
    if not isinstance(read, dict):
        return None
    return Tone(float(read["tone"]), bool(read["opaque"]), float(read["ratio"]))


async def _tones(assets: list[Asset], found: dict[str, Tone]) -> None:
    """Fill *found* with the tone of each of *assets*, one after the other; one not read is left out."""
    for asset in assets:
        if asset.id in found:
            continue
        try:
            tone = await _tone_of(asset)
        except Exception as exc:  # one picture unread: the others are still read
            logger.info("Figure picture %s not read for its tone: %s", asset.id, format_exception_message(exc))
            continue
        if tone is not None:
            found[asset.id] = tone


async def _readable(db: Any, file_id: str, user: Any) -> bool:
    from app.models.file_attachment import FileAttachment
    from app.services.file_access import can_read_file

    row = await db.get(FileAttachment, uuid.UUID(file_id))
    return row is not None and await can_read_file(row, user, db)


async def _drawn_as(
    db: Any, picture: Asset, names: dict[str, list[Asset]], tones: dict[str, Tone], *, scheme: str, user: Any
) -> tuple[Asset, Asset | None] | None:
    """What *picture* is drawn as on a figure of *scheme*, and the asset it went through to get there; ``None`` when
    it is drawn as placed. *names* are the assets each asset names or is named by (:func:`named`), *tones* their
    tones; only an asset *user* may read is drawn."""

    def scored(asset: Asset, *without: Asset) -> list[tuple[Asset, Tone]]:
        skipped = {asset.id, *(other.id for other in without)}
        return [(other, tones[other.id]) for other in names.get(asset.id, []) if other.id in tones and other.id not in skipped]

    mine = tones.get(picture.id)
    if mine is None:
        return None
    base, tone = picture, mine
    vector = successor(picture, mine, scored(picture))
    if vector is not None and await _readable(db, vector.id, user):
        base, tone = vector, tones[vector.id]
    pool = {other.id: (other, found) for other, found in (*scored(picture), *scored(base, picture))}
    twin = pick(tone, list(pool.values()), scheme)
    if twin is not None and not await _readable(db, twin.id, user):
        twin = None
    if twin is not None and not twin.vector:
        sharp = successor(twin, tones[twin.id], scored(twin, picture))
        if sharp is not None and await _readable(db, sharp.id, user):
            return sharp, twin
    if twin is not None:
        return twin, (base if base is not picture else None)
    return (base, None) if base is not picture else None


async def scheme_twins(db: Any, slide: dict, *, scheme: str, user: Any) -> dict[str, Twin]:
    """``file id as the slide spells it -> Twin`` of every template asset *slide* draws whose twin suits a figure of
    *scheme* better (:func:`pick`), or that is a raster with a vector successor (:func:`successor`), drawn as what
    *user* may read."""
    from app.services.video_studio.spec import figure_file_ids

    spelled: dict[uuid.UUID, list[str]] = defaultdict(list)
    for raw in sorted(figure_file_ids(slide))[:MAX_PICTURES]:
        uid = safe_uuid(raw)
        if uid is not None:
            spelled[uid].append(raw)
    pictures = await _template_assets(db, list(spelled)) if spelled else []
    if not pictures:
        return {}
    siblings = await _siblings(db, {picture.container for picture in pictures})
    names: dict[str, list[Asset]] = {picture.id: named(picture, siblings) for picture in pictures}
    # A raster's successor and its raster twins have twins and successors of their own: their names are read too.
    for picture in pictures:
        if picture.vector:
            continue
        for other in names[picture.id]:
            names.setdefault(other.id, named(other, siblings))
    wanted = list(
        {
            asset.id: asset
            for picture in pictures
            if names[picture.id]
            for asset in (
                picture,
                *names[picture.id],
                *(() if picture.vector else (far for near in names[picture.id] for far in names.get(near.id, []))),
            )
        }.values()
    )
    if not wanted:
        return {}
    tones: dict[str, Tone] = {}
    try:
        await asyncio.wait_for(_tones(wanted, tones), timeout=TONE_SECONDS)
    except TimeoutError:
        logger.info("Figure pictures not all read for their tone within %.0f s", TONE_SECONDS)
    out: dict[str, Twin] = {}
    for picture in pictures:
        found = await _drawn_as(db, picture, names, tones, scheme=scheme, user=user)
        if found is None:
            continue
        drawn, through = found
        for raw in spelled[uuid.UUID(picture.id)]:
            out[raw] = Twin(
                raw,
                drawn.id,
                picture.filename,
                drawn.filename,
                sharpened=drawn.vector and not picture.vector,
                through=through.filename if through is not None else "",
            )
    return out


def _swapped(value: Any, ids: dict[str, str]) -> Any:
    """*value* (a slide) with every ``*file_id`` of *ids* replaced, as ``spec.figure_file_ids`` reads them."""
    if isinstance(value, dict):
        return {
            key: ids.get(item, item) if key.endswith("file_id") and isinstance(item, str) else _swapped(item, ids)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_swapped(item, ids) for item in value]
    return value


async def themed(db: Any, source: Any, *, scheme: str, user: Any) -> Any:
    """*source* (a ``figures.FigureSource``) with each template asset of its slide drawn as its twin for *scheme*
    (:func:`scheme_twins`): its slide and the pictures it may draw follow, and it says which (``twins``). Never
    raises: a figure whose twins cannot be read is drawn as its slide places it."""
    from app.services.video_studio.spec import figure_file_ids

    try:
        twins = await scheme_twins(db, source.slide, scheme=scheme, user=user)
    except Exception as exc:
        logger.info("Figure twins not read: %s", format_exception_message(exc))
        return source
    if not twins:
        return source
    slide = _swapped(source.slide, {raw: twin.twin for raw, twin in twins.items()})
    return dataclasses.replace(source, slide=slide, files=frozenset(figure_file_ids(slide)), twins=tuple(twins.values()))
