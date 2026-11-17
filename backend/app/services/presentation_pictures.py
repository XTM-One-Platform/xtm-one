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

"""The pictures a deck places: where each one is, and what it is.

A picture on a slide is a platform file id in a ``file_id`` / ``*_file_id``
field: an ``image`` block, one logo of a ``logo_grid``, a ``profiles`` photo or
line logo, an ``architecture`` node or zone logo, a slide's background image, a title
slide's speaker photo. To an agent asked to replace "the outdated screenshot"
that id was a bare UUID: nothing said which file was which, and
``read_file_content`` only opens files attached to the CURRENT conversation,
while a deck's pictures are template assets or files of the conversation that
built it.

This module is the pure half of the answer, plus one loader:

* :func:`is_file_reference_key`, :func:`walk_file_references` and
  :func:`slide_file_references` are THE walk of the file contract, shared by
  the writer check (``presentations.verify_file_references``), the reader
  grant (``presentations.spec_references_file``), ``replace_text``
  (which never rewrites a file id) and ``replace_image`` (which swaps one):
  four consumers that must agree on which strings are pictures.
* :func:`picture_placements` names every place a slide shows a picture in
  ``edit_presentation``'s own address grammar (``block 1 (logo_grid) item 3``,
  ``speaker photo``), with what the slide says about it (alt text, label, name).
* :func:`load_picture_facts` says what each file is (filename, template role,
  caption, provenance) in one query - the filename and caption of a template
  asset only under the template's visibility, the name and notes of a
  conversation file only to its owner.

The list an agent reads is formatted with the tools
(``builtin_tools/presentation.py``).
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Any

from app.core.utils import is_valid_uuid
from app.services.presentation_markup import markup_to_plain_text_lenient

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

#: Slide-level picture fields, named the way a person says them.
_SLIDE_PICTURE_FIELDS: dict[str, str] = {
    "background_image_file_id": "background image",
}

#: What a slide says about the picture a field holds, read on the object that
#: holds the field: an image's alt text (or its caption), a logo's label, a
#: person's name, a profile line's text, an architecture node's label.
_CONTEXT_KEYS: tuple[str, ...] = ("alt", "label", "name", "text", "caption")

#: How much of that context a place quotes: enough to tell two pictures apart.
_CONTEXT_MAX_CHARS = 60

#: The spellings of a file id a model copies from other tool results: the
#: ``[[FILE:<id>]]`` marker, the ``file:<id>`` URL form, a ``file-<id>`` handle.
_FILE_ID_SPELLINGS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\[\[FILE:([^\]\s]+)\]\]", re.IGNORECASE),
    re.compile(r"file[:-]([0-9a-f-]{32,36})", re.IGNORECASE),
)


def is_file_reference_key(key: object) -> bool:
    """Whether a spec field holds a platform file id: ``file_id`` or ``*_file_id``."""
    return isinstance(key, str) and (key == "file_id" or key.endswith("_file_id"))


def canonical_file_reference(value: str) -> str:
    """Compare file references the way the rows are looked up: a UUID by value
    (``ABC...`` and ``abc...`` are one file), anything else verbatim."""
    return str(uuid.UUID(value)) if is_valid_uuid(value) else value


def normalize_file_reference(value: str) -> str:
    """*value* as the spec compares it, whichever spelling the model copied."""
    text = value.strip()
    for pattern in _FILE_ID_SPELLINGS:
        match = pattern.fullmatch(text)
        if match:
            text = match.group(1)
            break
    return canonical_file_reference(text)


def walk_file_references(node: object, path: str, found: list[tuple[str, str]]) -> None:
    """Collect ``(dotted path, file id)`` for every ``file_id`` / ``*_file_id``."""
    if isinstance(node, dict):
        for key, value in node.items():
            here = f"{path}.{key}" if path else key
            if isinstance(value, str) and is_file_reference_key(key):
                found.append((here, value))
            else:
                walk_file_references(value, here, found)
    elif isinstance(node, list):
        for index, item in enumerate(node):
            walk_file_references(item, f"{path}.{index}", found)


def slide_file_references(spec: object) -> list[tuple[str, str | None, str]]:
    """``(dotted path, slide layout, file id)`` for every file a deck's SLIDES name.

    The one walk behind both halves of the file contract: what
    ``presentations.verify_file_references`` checks against the WRITER when a
    reference is introduced, and what ``presentations.spec_references_file``
    grants to a READER of the deck afterwards. Keeping them on the same
    walk is what makes the grant sound: a field this walk does not see is
    neither vouched for nor served. Only ``slides`` is walked: the logo a
    template contributes lives in the container's config and is
    visibility-checked by ``presentations.resolve_template_theme`` on every
    read.

    The layout travels separately so a structured problem can render it for
    either reader (the agent line and the human line).
    """
    references: list[tuple[str, str | None, str]] = []
    if not isinstance(spec, dict):
        return references
    for index, slide in enumerate(spec.get("slides") or []):
        if isinstance(slide, dict):
            found: list[tuple[str, str]] = []
            walk_file_references(slide, f"slides.{index}", found)
            layout = slide.get("layout")
            references.extend((path, layout if isinstance(layout, str) else None, value) for path, value in found)
    return references


def slide_file_ids(spec: object) -> set[str]:
    """The canonical file ids a deck's slides name (see :func:`canonical_file_reference`)."""
    return {canonical_file_reference(value) for _path, _layout, value in slide_file_references(spec)}


@dataclass(frozen=True, slots=True)
class PicturePlacement:
    """One place a slide shows a picture."""

    #: 0-based position of the slide in the deck.
    slide_index: int
    slide_id: str | None
    layout: str | None
    #: Where on the slide, in ``edit_presentation``'s address grammar.
    where: str
    #: The file id as stored.
    file_id: str
    #: What the slide says about the picture (alt text, label, name); "" when nothing.
    context: str = ""

    @property
    def key(self) -> str:
        """The file id as rows are looked up (see :func:`canonical_file_reference`)."""
        return canonical_file_reference(self.file_id)

    def label(self, unit: str = "slide") -> str:
        """``slide 9 block 1 (logo_grid) item 3 "Acme"``: the place as a result names it."""
        text = f"{unit} {self.slide_index + 1} {self.where}"
        return f'{text} "{self.context}"' if self.context else text


def picture_placements(spec: object, slide_indexes: Iterable[int] | None = None) -> list[PicturePlacement]:
    """Every place the slides of *spec* show a picture, in deck order.

    *slide_indexes* (0-based) narrows the walk to the slides a tool result
    serves; ``None`` walks every slide.
    """
    if not isinstance(spec, dict):
        return []
    slides = spec.get("slides") or []
    wanted = None if slide_indexes is None else set(slide_indexes)
    placements: list[PicturePlacement] = []
    for path, layout, value in slide_file_references(spec):
        _slides, _, rest = path.partition(".")
        index_text, _, inner = rest.partition(".")
        index = int(index_text)
        if wanted is not None and index not in wanted:
            continue
        slide = slides[index]
        where, context = describe_location(slide, inner.split("."))
        slide_id = slide.get("id")
        placements.append(
            PicturePlacement(
                slide_index=index,
                slide_id=slide_id if isinstance(slide_id, str) else None,
                layout=layout,
                where=where,
                file_id=value,
                context=context,
            )
        )
    return placements


def describe_location(slide: Mapping[str, Any], segments: Sequence[str]) -> tuple[str, str]:
    """``(where, context)`` for the file field at *segments* inside *slide*.

    *where* speaks ``edit_presentation``'s block address grammar
    (``presentation_patch``): ``block 3``, ``block 2.3`` on a columns slide,
    ``block 3.2`` inside a callout, ``block 3.2.1`` inside a columns block,
    ``block 4.left.1`` in a comparison panel, ``block footer`` - so the place a
    list names is the address an operation takes. The part of the path inside
    the block reads ``item 3``, ``item 2 line 1 logo_file_id``, ``node 4
    logo_file_id``; the plain ``file_id`` of an ``image`` block is the block
    itself. A shape this does not know is named by its raw path, never dropped.
    """
    if not segments:
        return "", ""
    field = segments[-1]
    if len(segments) == 1:
        return _SLIDE_PICTURE_FIELDS.get(field, field), ""
    if len(segments) == 3 and segments[0] == "speakers" and segments[1].isdigit():
        # A title slide's speaker portrait: `speaker 2 photo "Ada"`.
        speakers = slide.get("speakers")
        index = int(segments[1])
        speaker = speakers[index] if isinstance(speakers, list) and index < len(speakers) else None
        context = _plain(speaker.get("name")) if isinstance(speaker, Mapping) else ""
        return f"speaker {index + 1} photo", context
    try:
        address, block, inner = _block_address(slide, list(segments))
        holder = _holder(block, inner)
    except (LookupError, ValueError, TypeError):
        return ".".join(segments), ""
    where = f"block {'.'.join(address)} ({block.get('type')})"
    detail = _describe_inner(inner)
    if detail:
        where = f"{where} {detail}"
    return where, _quote_picture_context(holder, field) or _context_of(holder)


#: A quote's pictures are named by its credit: the portrait by the
#: author, the logo by the organization, either by the legacy attribution.
_QUOTE_PICTURE_CREDIT: dict[str, tuple[str, ...]] = {
    "photo_file_id": ("author", "attribution"),
    "logo_file_id": ("organization", "attribution"),
}


def _quote_picture_context(holder: Any, field: str) -> str:
    if not isinstance(holder, Mapping) or holder.get("type") != "quote":
        return ""
    for key in _QUOTE_PICTURE_CREDIT.get(field, ()):
        text = _plain(holder.get(key))
        if text:
            return text
    return ""


def _block_address(slide: Mapping[str, Any], segments: list[str]) -> tuple[list[str], Mapping[str, Any], list[str]]:
    """The block address of *segments*, the block they end in, and the path left inside it."""
    head = segments[0]
    if head == "blocks":
        index = int(segments[1])
        address = [str(index + 1)]
        node: Any = slide["blocks"][index]
        rest = segments[2:]
    elif head == "columns":
        column, index = int(segments[1]), int(segments[2])
        address = [str(column + 1), str(index + 1)]
        node = slide["columns"][column][index]
        rest = segments[3:]
    elif head == "footer_block":
        address = ["footer"]
        node = slide["footer_block"]
        rest = segments[1:]
    else:
        raise LookupError(head)
    # Step into nested blocks the way ``resolve_block_slot`` addresses them.
    while len(rest) > 2 and isinstance(node, Mapping):
        key = rest[0]
        if key == "children" and isinstance(node.get("children"), list):
            index = int(rest[1])
            address.append(str(index + 1))
            node, rest = node["children"][index], rest[2:]
        elif key == "columns" and node.get("type") == "columns" and len(rest) > 3:
            column, index = int(rest[1]), int(rest[2])
            address += [str(column + 1), str(index + 1)]
            node, rest = node["columns"][column][index], rest[3:]
        elif key in ("left", "right") and node.get("type") == "comparison" and rest[1] == "blocks" and len(rest) > 3:
            index = int(rest[2])
            address += [key, str(index + 1)]
            node, rest = node[key]["blocks"][index], rest[3:]
        else:
            break
    if not isinstance(node, Mapping):
        raise LookupError("not a block")
    return address, node, rest


def _holder(block: Mapping[str, Any], inner: Sequence[str]) -> Any:
    """The object inside *block* that holds the field *inner* ends with."""
    node: Any = block
    for segment in inner[:-1]:
        node = node[int(segment)] if isinstance(node, list) else node[segment]
    return node


def _describe_inner(inner: Sequence[str]) -> str:
    """``item 2 line 1 logo_file_id``: the path inside a block, 1-based."""
    parts: list[str] = []
    index = 0
    while index < len(inner) - 1:
        key, following = inner[index], inner[index + 1]
        if following.isdigit():
            parts.append(f"{key[:-1] if key.endswith('s') else key} {int(following) + 1}")
            index += 2
        else:
            parts.append(key)
            index += 1
    if inner and inner[-1] != "file_id":
        parts.append(inner[-1])
    return " ".join(parts)


def _context_of(holder: Any) -> str:
    if not isinstance(holder, Mapping):
        return ""
    for key in _CONTEXT_KEYS:
        text = _plain(holder.get(key))
        if text:
            return text
    return ""


def _plain(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    text = " ".join(markup_to_plain_text_lenient(value).split()).replace('"', "'")
    if len(text) > _CONTEXT_MAX_CHARS:
        text = text[: _CONTEXT_MAX_CHARS - 3].rstrip() + "..."
    return text


@dataclass(frozen=True, slots=True)
class PictureFacts:
    """What one platform file is, as a deck's reader may be told."""

    file_id: str
    #: ``None`` when the name is not the reader's to be told (see
    #: :func:`load_picture_facts`): the file is shown, not described.
    filename: str | None
    content_type: str
    size: int
    #: ``template asset``, ``uploaded`` or ``generated``.
    origin: str
    #: The template asset role (``screenshot``, ``customer_logo``, ...).
    role: str | None = None
    #: A template asset's caption, or the notes an agent saved on the file;
    #: withheld with the filename.
    description: str | None = None
    #: The template the asset belongs to, only when the reader may see it.
    template_id: str | None = None
    template_name: str | None = None
    added: date | None = None

    @property
    def withheld(self) -> bool:
        """Whether this reader is told that the file exists, not what it is called."""
        return self.filename is None


async def load_picture_facts(
    db: AsyncSession,
    file_ids: Iterable[str],
    *,
    user_id: uuid.UUID | None,
    group_ids: Sequence[str] | None = None,
    is_admin: bool = False,
) -> dict[str, PictureFacts]:
    """What each of *file_ids* is, keyed by canonical id; an id no row carries is absent.

    Metadata of files the deck's reader may already fetch (a deck grants the
    PIXELS its slides show) - but not everything a row says is the
    reader's. A template asset's filename, caption and template are told under
    the same three-tier visibility as every other template read (own + shared
    + company-managed): an asset of someone's private template is named by its
    role, never its template. *is_admin* is the admin bypass the REST deck
    response takes for its template, palette and name alike; the agent tools
    never pass it. A conversation file's name and
    the notes an agent saved on it (``annotate_file``) are its OWNER's: another
    reader learns it is an uploaded or generated file, its size and date. One
    query for the rows, one for the visible templates, and none at all without
    a UUID to look up.
    """
    from sqlalchemy import select

    from app.llm.base import FileRef
    from app.models.file_attachment import FileAttachment
    from app.models.template_container import TEMPLATE_ASSET_SOURCE

    wanted = {uuid.UUID(value) for value in file_ids if is_valid_uuid(value)}
    if not wanted:
        return {}
    rows = (await db.execute(select(FileAttachment).where(FileAttachment.id.in_(wanted)))).scalars().all()
    names: dict[uuid.UUID, str] = {}
    container_ids = {row.template_container_id for row in rows if row.template_container_id is not None}
    if container_ids and user_id is not None:
        from app.api.v1.helpers import user_visible_filter
        from app.models.template_container import TemplateContainer

        result = await db.execute(
            select(TemplateContainer.id, TemplateContainer.name).where(
                TemplateContainer.id.in_(container_ids),
                user_visible_filter(TemplateContainer, user_id, list(group_ids or []), is_admin=is_admin),
            )
        )
        names = {row_id: name for row_id, name in result.all()}
    facts: dict[str, PictureFacts] = {}
    for row in rows:
        container_id = row.template_container_id
        visible_name = names.get(container_id) if container_id is not None else None
        if container_id is not None or row.source == TEMPLATE_ASSET_SOURCE:
            origin = "template asset"
            told = visible_name is not None
        else:
            origin = "generated" if FileRef.from_attachment(row).is_agent_generated else "uploaded"
            told = user_id is not None and getattr(row, "user_id", None) == user_id
        created = getattr(row, "created_at", None)
        facts[str(row.id)] = PictureFacts(
            file_id=str(row.id),
            filename=row.filename if told else None,
            content_type=row.content_type or "",
            size=int(row.size or 0),
            origin=origin,
            role=row.asset_role or None,
            description=((row.description or "").strip() or None) if told else None,
            template_id=str(container_id) if visible_name is not None else None,
            template_name=visible_name,
            added=created.date() if created is not None else None,
        )
    return facts


#: The pictures of a ``quote`` block: a portrait and a company logo
#: beside a credit that may or may not name them.
QUOTE_PICTURE_FIELDS: tuple[str, ...] = ("photo_file_id", "logo_file_id")


def quote_picture_ids(spec: object) -> list[str]:
    """Every file id a ``quote`` block of *spec* shows as a picture, wherever the quote sits."""
    found: list[str] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            if node.get("type") == "quote":
                found.extend(
                    value for key in QUOTE_PICTURE_FIELDS if isinstance(value := node.get(key), str) and value.strip()
                )
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    if isinstance(spec, dict):
        walk(spec.get("slides"))
    return found


async def load_quote_picture_labels(
    db: AsyncSession,
    spec: object,
    *,
    user_id: uuid.UUID | None,
    group_ids: Sequence[str] | None = None,
    is_admin: bool = False,
) -> dict[str, dict[str, str | None]]:
    """What the renderer may name a quote picture by, keyed by canonical file id.

    A quote picture that no visible credit names takes its alt text from the
    file: its description, else its filename. Served with the deck for the
    quote pictures its spec references only, under :func:`load_picture_facts`'
    rules - a withheld file is left out and the renderer falls back to a
    generic label. No query without a quote picture.
    """
    facts = await load_picture_facts(
        db, quote_picture_ids(spec), user_id=user_id, group_ids=group_ids, is_admin=is_admin
    )
    return {
        file_id: {"description": fact.description, "filename": fact.filename}
        for file_id, fact in facts.items()
        if fact.description or fact.filename
    }
