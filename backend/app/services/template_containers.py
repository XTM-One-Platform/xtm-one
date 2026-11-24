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

"""Template container service — structured (non-vector) template lookup.

Single source of truth for **how a template is found**: exact / prefix filters on
structured metadata (name, kind, tag, colour scheme, aspect ratio), shared by the
REST API (``api/v1/template_containers.py``) and the agent-facing
``search_templates`` built-in tool, so the two can never drift on visibility or
match semantics.

There is deliberately **no embedding, chunking or vector similarity anywhere in
this module** — that is the whole point. A template is retrieved by the
attributes a consumer feature actually filters on ("the 16:9 dark presentation
deck"), not by fuzzy semantic proximity. Nothing here touches
``app.services.vector``, ``app.services.chunking`` or the ``embeddings`` table,
and ``search_knowledge`` has no path into these rows.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import uuid

from sqlalchemy import Text, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.file_attachment import FileAttachment
from app.models.template_container import (
    MAX_ASSET_DESCRIPTION_CHARS,
    TEMPLATE_ASSET_SOURCE,
    TemplateContainer,
)
from app.services.file_responses import file_version

logger = logging.getLogger(__name__)

#: Hard cap on rows a single structured search can return, so a broad filter
#: can never dump the whole table into an agent's context window.
MAX_SEARCH_RESULTS = 50


def normalize_asset_description(value: object) -> str | None:
    """Normalize an asset description for STORAGE: one line, trimmed, bounded.

    The description is what an agent reads to choose an asset, and it is quoted
    into the ``get_template`` tool result one asset per line — so a newline in
    it would break that listing, and an unbounded one would let a single asset
    crowd the rest out of the context window. Whitespace runs collapse to one
    space; anything that is not a non-empty string becomes ``None`` (no
    description), never an empty string. Shared by the REST routes, the package
    import, the startup seed and the authoring tool so they agree on what lands.
    """
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split())
    if not cleaned:
        return None
    return cleaned[:MAX_ASSET_DESCRIPTION_CHARS]


def serialize_asset(attachment: FileAttachment) -> dict:
    """Serialize one template asset to a JSON-safe dict.

    Deliberately omits ``s3_object_name``: the object key is an internal
    storage detail, and agents/consumers address assets by ``id`` through the
    download endpoint. ``version`` is the download's ``ETag`` without quotes:
    it changes when the asset is replaced in place, so a client that
    keys its own cache on the URL puts it there to pick up the new bytes.
    """
    from app.services.image_geometry import render_geometry

    return {
        "id": str(attachment.id),
        "filename": attachment.filename,
        "content_type": attachment.content_type,
        "size": attachment.size,
        "asset_role": attachment.asset_role,
        "description": attachment.description,
        "version": file_version(attachment),
        # The trim box a logo wall sizes the mark by - the template
        # preview draws the brand's logos with it, like a deck does. Its
        # `rev` is a digest of the storage key, never the key itself, served
        # once the file was read as an image (a font carries no image_meta).
        "image_meta": render_geometry(
            getattr(attachment, "image_meta", None),
            storage_key=(
                getattr(attachment, "s3_object_name", None)
                if getattr(attachment, "image_meta", None) is not None
                else None
            ),
        ),
        "created_at": (
            attachment.created_at.isoformat() if attachment.created_at else None
        ),
    }


def is_presentation_kind(kind: object) -> bool:
    """Whether *kind* is the deck surface - the only kind that dresses a presentation."""
    return isinstance(kind, str) and kind.strip().lower() == PRESENTATION_KIND


def has_color_schemes(kind: object) -> bool:
    """Whether a template of *kind* dresses its surface in a dark and / or a light scheme (decks, videos)."""
    return isinstance(kind, str) and kind.strip().lower() in (PRESENTATION_KIND, VIDEO_KIND)


def container_supported_schemes(container) -> list[str]:
    """The colour schemes (``dark`` / ``light``) *container* can dress a deck in.

    The template's declaration (``extra_metadata.supported_schemes``)
    when it makes one, else what its palettes can dress
    (:func:`~app.services.presentations.template_supported_schemes`) - so the
    Templates UI, the deck editor's template picker and the ``theme`` search
    filter all read the same list without knowing either rule. A video template
    declares its schemes the same way, for the videos it dresses. A
    container of any other kind (a report skin, a legacy asset library)
    dresses no scheme and gets an empty list: a scheme filter never returns it.
    """
    from app.services.presentations import template_supported_schemes

    if not has_color_schemes(getattr(container, "kind", None)):
        return []
    return template_supported_schemes(
        getattr(container, "theme_config", None), getattr(container, "extra_metadata", None)
    )


def serialize_container(
    container: TemplateContainer,
    *,
    assets: list[FileAttachment] | None = None,
    asset_count: int | None = None,
) -> dict:
    """Serialize a container to a JSON-safe dict.

    ``assets`` includes the full asset list; ``asset_count`` alone is enough for
    list views. Passing ``assets`` derives the count from it.

    ``supported_schemes`` is :func:`container_supported_schemes`: what
    the deck editor's theme picker is restricted to once this template is
    linked, and empty for a container that is not a presentation template.
    ``theme`` is the legacy free-text search label, served for compatibility;
    nothing renders it (the colours live in ``theme_config``) and the UI no
    longer writes it.
    """
    payload = {
        "id": str(container.id),
        "user_id": str(container.user_id) if container.user_id else None,
        "name": container.name,
        "description": container.description,
        "kind": container.kind,
        "tags": container.tags or [],
        "theme": container.theme,
        "theme_config": container.theme_config or {},
        "supported_schemes": container_supported_schemes(container),
        "aspect_ratio": container.aspect_ratio,
        "layouts": container.layouts or [],
        "extra_metadata": container.extra_metadata or {},
        "lookup_count": container.lookup_count,
        "is_company_managed": container.is_company_managed,
        # Code-managed and re-synced on every boot: the UI shows it
        # read-only with a Duplicate action, and the REST write surface refuses
        # to mutate it, so the flag is part of the contract rather than a tag
        # the client would have to know about.
        "is_builtin": is_builtin_container(container),
        "shared_group_ids": container.shared_group_ids,
        "shared_group_permissions": container.shared_group_permissions,
        "created_at": (
            container.created_at.isoformat() if container.created_at else None
        ),
        "updated_at": (
            container.updated_at.isoformat() if container.updated_at else None
        ),
    }
    if assets is not None:
        payload["assets"] = [serialize_asset(a) for a in assets]
        payload["asset_count"] = len(assets)
    else:
        payload["asset_count"] = asset_count or 0
    return payload


async def load_role_asset_ids(
    db: AsyncSession, container_id: uuid.UUID, roles: list[str]
) -> dict[str, uuid.UUID]:
    """The oldest IMAGE asset of a container for each of *roles*, by role.

    This is the "assets by role" convention: the deck chrome draws the
    asset tagged ``logo`` (``logo_light`` on the light theme) without the theme
    config having to name a file id. Oldest-first makes the pick stable when a
    role has several assets; a non-image asset with a logo role (a PDF brand
    book someone tagged ``logo``) is never returned, because the renderer would
    put it in an ``<img>``.
    """
    if not roles:
        return {}
    result = await db.execute(
        select(FileAttachment.id, FileAttachment.asset_role)
        .where(
            FileAttachment.template_container_id == container_id,
            FileAttachment.source == TEMPLATE_ASSET_SOURCE,
            FileAttachment.asset_role.in_(roles),
            FileAttachment.content_type.like("image/%"),
        )
        .order_by(FileAttachment.created_at.asc())
    )
    found: dict[str, uuid.UUID] = {}
    for asset_id, role in result.all():
        if role not in found:
            found[role] = asset_id
    return found


async def load_assets(
    db: AsyncSession, container_id: uuid.UUID
) -> list[FileAttachment]:
    """Load a container's assets, oldest first.

    Filters on BOTH the FK and ``source`` so a row that somehow lost its
    ``template_asset`` source is never served as a template asset.
    """
    result = await db.execute(
        select(FileAttachment)
        .where(
            FileAttachment.template_container_id == container_id,
            FileAttachment.source == TEMPLATE_ASSET_SOURCE,
        )
        .order_by(FileAttachment.created_at.asc())
    )
    return list(result.scalars().all())


async def load_visible_asset(file_id: object, user_id: object) -> FileAttachment | None:
    """The template asset *file_id* names when *user_id* can see its container, else ``None``.

    The grant ``get_template``, the file download and ``render_html_to_image`` apply: the
    container own, shared with the caller or a group, or company-managed
    (``caller_visible_filter``). A full id only; a malformed id, a row that is no template
    asset and a container the caller cannot see all answer ``None``. Never raises.
    """
    from app.api.v1.helpers import caller_visible_filter, get_user_group_ids
    from app.core.database import async_session
    from app.core.utils import safe_uuid

    asset_id = safe_uuid(str(file_id).strip()) if isinstance(file_id, str) else None
    user_uuid = safe_uuid(str(user_id)) if user_id else None
    if asset_id is None or user_uuid is None:
        return None
    try:
        async with async_session() as db:
            group_ids = await get_user_group_ids(db, user_uuid)
            return (
                (
                    await db.execute(
                        select(FileAttachment)
                        .join(TemplateContainer, TemplateContainer.id == FileAttachment.template_container_id)
                        .where(
                            FileAttachment.id == asset_id,
                            FileAttachment.source == TEMPLATE_ASSET_SOURCE,
                            caller_visible_filter(TemplateContainer, user_uuid, group_ids),
                        )
                    )
                )
                .scalars()
                .first()
            )
    except Exception as exc:
        logger.warning("Template asset %s not loaded: %s", asset_id, exc)
        return None


def template_asset_prefix(container_id: uuid.UUID) -> str:
    """The object-key prefix every asset of *container_id* is stored under."""
    return f"template-asset/{container_id}"


async def store_template_asset_bytes(
    container_id: uuid.UUID,
    data: bytes,
    *,
    filename: str,
    content_type: str,
) -> tuple[bytes, str]:
    """Write the bytes of a template asset. Returns ``(stored bytes, object key)``.

    The ONE path through which bytes become a template asset object:
    REST upload and replace, package import, duplication and the agent's
    ``attach_template_asset`` all call this, so an SVG is sanitized exactly
    once, on the way into storage, wherever it came from — `render points never
    have to trust the bucket`. ``stored bytes`` may differ from *data* for an
    SVG (the caller records ITS length as the asset size, not the upload's).

    Raises :class:`app.services.svg_sanitizer.SvgSanitizeError` for an SVG
    that cannot be parsed; the caller maps it to its own refusal. The brand
    seed does not use this (it needs a deterministic key) but runs the same
    sanitizer, which ``tests/test_template_containers.py`` pins.
    """
    from app.services.file_storage import file_storage
    from app.services.svg_sanitizer import sanitize_template_asset

    clean = sanitize_template_asset(data, content_type, filename)
    key = await file_storage.upload(
        data=clean,
        filename=filename,
        content_type=content_type,
        prefix=template_asset_prefix(container_id),
    )
    return clean, key


async def template_asset_sha256(stored: bytes) -> str:
    """The ``FileAttachment.sha256`` of a template asset: the digest of the bytes
    :func:`store_template_asset_bytes` STORED (the sanitized SVG, never the upload).

    Every write that points a template asset row at an object records it, a
    replace in place included: the presentation package import trusts the
    column to tell a template's same-name assets apart without reading them
    (``tests/test_file_attachment_digest.py``). Hashed in a worker
    thread, since an asset can be as large as the upload cap.
    """
    return await asyncio.to_thread(lambda: hashlib.sha256(stored).hexdigest())


async def count_assets_by_container(
    db: AsyncSession, container_ids: list[uuid.UUID]
) -> dict[str, int]:
    """Batch asset counts for a list of containers (avoids an N+1 in list views)."""
    if not container_ids:
        return {}
    result = await db.execute(
        select(
            FileAttachment.template_container_id,
            func.count(FileAttachment.id),
        )
        .where(
            FileAttachment.template_container_id.in_(container_ids),
            FileAttachment.source == TEMPLATE_ASSET_SOURCE,
        )
        .group_by(FileAttachment.template_container_id)
    )
    return {str(cid): count for cid, count in result.all()}


def normalize_tags(tags: list[str] | None) -> list[str]:
    """Trim, lower-case and de-blank tags for STORAGE.

    This is the write-side counterpart of the tag predicate in
    ``apply_structured_filters``, and it lives next to it on purpose: the filter
    matches a lower-cased, quoted token against the JSON text (``'"executive"'``),
    so a stored tag of ``" Executive "`` would serialise as ``" Executive "`` and
    become permanently unsearchable. Normalising at both ends is what keeps
    structured search predictable — if the two ever drift, tags silently stop
    matching, which is why callers must use this rather than inlining the
    comprehension.

    Order is preserved and duplicates that collapse after normalisation are
    dropped (``["A", "a"]`` -> ``["a"]``), so a tag list can't hold the same
    effective tag twice.

    The seed's IDENTITY tags are dropped as well: ``builtin`` and any
    ``slug:<slug>`` tag are how ``template_container_seed`` recognises the rows
    it owns, so a container carrying them is read-only on every write surface
    and is full-synced (or deleted as stale) on the next boot. Every write path
    that takes tags from a user, an agent, an import or a duplicate goes through
    here, which is what keeps that fate reserved for rows the seed created:
    a duplicated brand deck must be editable, an imported export of one must
    not be mistaken for the original, and an admin typing ``builtin`` into a
    tag field must not lock their own template forever. The seed writes its
    tags directly and never calls this.
    """
    from app.services.template_container_seed import BUILTIN_TAG, SLUG_TAG_PREFIX

    seen: dict[str, None] = {}
    for tag in tags or []:
        if not tag or not tag.strip():
            continue
        cleaned = tag.strip().lower()
        if cleaned == BUILTIN_TAG or cleaned.startswith(SLUG_TAG_PREFIX):
            continue
        seen.setdefault(cleaned, None)
    return list(seen)


# ---------------------------------------------------------------------------
# Authoring guardrails
#
# The REST surface trusts its caller: a human editing a container in the
# Templates UI can see the deck they broke and fix it. An agent authoring a
# container from a conversation cannot, so the two ways a template silently does
# nothing are turned into reported errors here rather than stored values:
#
# * a ``theme_config`` key outside ``THEME_CONFIG_KEYS`` is dropped by
#   ``sanitize_theme_config`` at render time — the palette just doesn't apply;
# * a layout id outside ``SLIDE_LAYOUTS`` promises a slide the validator
#   refuses (the retired v1 ids, are dropped rather than reported).
#
# Both lists are imported from the modules that enforce them, never re-spelled,
# so a guardrail cannot drift from its validator.
#
# That trust covers what does NOTHING, never what does harm: a palette value the
# value guard refuses (a CSS function that can load a URL in every viewer's
# browser) is refused on the REST routes too, through
# ``unsafe_theme_value_problems``.
# ---------------------------------------------------------------------------

#: The two surfaces a template dresses: a presentation deck or page and
#: an executive report. Each has a renderer contract - the palette keys
#: it honours (:func:`theme_config_keys_for_kind`) and the layout / block ids
#: its catalog may advertise (:func:`validate_layouts`).
PRESENTATION_KIND = "presentation"
REPORT_KIND = "report"
#: A video template: the deck palette and fonts, its colour schemes,
#: and the video rules in ``extra_metadata.video`` (``video_studio.templates``).
VIDEO_KIND = "video"

#: The kinds a template may be CREATED with: a closed choice, offered as
#: "Presentation" / "Report" on the template form and refused for anything else
#: by the REST create / update routes and the agent's ``create_template`` /
#: ``update_template`` (:func:`normalize_kind`). "Kind" used to be a free-text
#: field with ``dashboard`` and ``asset-library`` suggestions, which asked the
#: user to classify something they had no model for - a template is a template:
#: its assets are readable, its colours and fonts inherited, whichever surface
#: it dresses. Rows of a retired kind keep opening and working (an existing
#: report skin stays a report skin); they just cannot be created anymore.
TEMPLATE_KINDS: tuple[str, ...] = (PRESENTATION_KIND, REPORT_KIND, VIDEO_KIND)

#: The retired container kind of the seeded logo packs, kept as a name
#: so the code that still recognises an EXISTING row of that kind - the list
#: badge, the seed's one-time merge into the brand deck - can spell it once.
#: Never creatable, never seeded, never advertised to agents: the
#: partner / integration / customer logos are predefined assets of the brand
#: deck itself (roles ``partner_logo`` / ``integration_logo`` /
#: ``customer_logo``), where a template's assets belong.
ASSET_LIBRARY_KIND = "asset-library"


def normalize_kind(raw: object) -> str | None:
    """The canonical kind for *raw*, or ``None`` when a template cannot be created with it.

    Blank means the default (``presentation``); anything else must spell one
    of :data:`TEMPLATE_KINDS`, case-insensitively. Used by every write path
    that sets a kind, so the REST routes and the agent tool refuse the same
    values with the same list.
    """
    text = str(raw or "").strip().lower()
    if not text:
        return PRESENTATION_KIND
    return text if text in TEMPLATE_KINDS else None


def kind_problem(raw: object) -> str:
    """The one sentence every surface uses to refuse a kind a template cannot be created with."""
    return (
        f"kind must be one of {', '.join(TEMPLATE_KINDS)} (got {str(raw or '').strip()!r}). "
        "A template dresses a presentation deck / page, a report or a video; "
        "its assets are readable whichever it is."
    )


def resolve_kind_update(stored: object, requested: object) -> str | None:
    """The kind a row ends up with when *requested* is written over *stored*, or ``None`` to refuse.

    An ECHO of the stored kind - equal once trimmed and lower-cased - is a
    no-op whatever the value: the row keeps its kind, a retired one
    (``asset-library``, an older ``dashboard``) included. The template form
    sends the kind back on every save, and ``TemplateKindSelect`` lists a
    legacy value in place precisely so it CAN be echoed; refusing the echo
    would make a legacy row unable to save its name, its tags or its palettes
    at all. A supported kind stored under another spelling (a free-text
    ``Presentation`` from before the closed choice) comes back canonical.

    Only a CHANGE is held to :data:`TEMPLATE_KINDS`, through
    :func:`normalize_kind` (blank still means ``presentation``); the caller
    turns ``None`` into :func:`kind_problem`. Shared by the REST update and the
    agent's ``update_template`` so the two cannot drift on what an echo means.
    """
    stored_text = str(stored or "").strip()
    if str(requested or "").strip().lower() == stored_text.lower():
        return normalize_kind(stored_text) or stored_text
    return normalize_kind(requested)


def theme_config_keys_for_kind(kind: str | None) -> tuple[str, ...]:
    """Return the ``theme_config`` whitelist the renderer of *kind* honours.

    A report needs semantic risk colours and document surfaces a slide has no use
    for, so the two renderers honour different key sets. Resolving that
    here — from the modules that enforce it — is what keeps the authoring
    guardrail from telling an author that ``critical`` "is not honoured" when the
    report renderer honours it perfectly well.
    """
    from app.services.presentations import THEME_CONFIG_KEYS
    from app.services.reports import REPORT_THEME_CONFIG_KEYS

    if (kind or "").strip().lower() == REPORT_KIND:
        return REPORT_THEME_CONFIG_KEYS
    return THEME_CONFIG_KEYS


def validate_theme_config(
    raw: object, *, kind: str | None = None
) -> tuple[dict[str, str], list[str]]:
    """Split a proposed ``theme_config`` into what is honoured and what is not.

    Returns ``(accepted, problems)``. ``accepted`` is what should be stored;
    ``problems`` is one human-readable line per rejected entry, meant to be
    handed straight back to the agent so it can correct the call rather than
    ship a palette that renders as nothing.

    *kind* selects the whitelist (see :func:`theme_config_keys_for_kind`); it
    defaults to the deck's, which is the narrower of the two.
    """
    from app.services.presentations import sanitize_theme_config

    if raw is None:
        return {}, []
    if not isinstance(raw, dict):
        return {}, [
            "theme_config must be a JSON object mapping theme keys to values, "
            f"got {type(raw).__name__}."
        ]

    keys = theme_config_keys_for_kind(kind)
    accepted = sanitize_theme_config(raw, keys=keys)
    problems: list[str] = []
    for key, value in raw.items():
        if key in accepted:
            continue
        if key not in keys:
            problems.append(
                f"theme_config key '{key}' is not honoured by the renderer and "
                f"was not stored — the only accepted keys are: "
                f"{', '.join(keys)}."
            )
            continue
        problem = _theme_value_refusal(key, value)
        if problem is not None:
            problems.append(problem)
    return accepted, problems


def _theme_value_refusal(key: str, value: object, *, label: str = "theme_config") -> str | None:
    """The sentence refusing *value* for the honoured key *key*, or ``None`` when it is accepted.

    One wording for every write path - the authoring tool, the import, the
    generator (through :func:`validate_theme_config`) and the REST routes
    (through :func:`unsafe_theme_value_problems`) - with *label* naming the
    palette the value came from (``theme_config`` or a ``theme_variants``
    palette). The value guard itself is ``presentations.theme_value_problem``,
    the same one every read runs, so what an author is told is exactly what a
    deck would drop.
    """
    from app.services.presentations import (
        MAX_THEME_VALUE_CHARS,
        THEME_FILE_ID_KEYS,
        THEME_FILE_ID_ROLES,
        theme_value_problem,
        trim_theme_value,
    )

    reason = theme_value_problem(key, value)
    if reason is None:
        return None
    if key in THEME_FILE_ID_KEYS:
        return (
            f"{label}['{key}'] must be the UUID (8-4-4-4-12 hex digits) of one of "
            "this template's image assets (attach the file first, then use its id), got "
            f"{value!r}. Leave it out to use the asset tagged "
            f"'{THEME_FILE_ID_ROLES.get(key, 'logo')}' automatically."
        )
    if not isinstance(value, str) or not trim_theme_value(value):
        return (
            f"{label}['{key}'] must be a non-empty string "
            "(a CSS colour or font-family), got "
            f"{type(value).__name__}."
        )
    # Passed the whitelist but failed the value guard: a function that can make
    # the browser load a URL, or a character that would let the value
    # escape the single CSS declaration it is interpolated into.
    return (
        f"{label}['{key}'] = {value!r} is not a plain colour or font-family "
        f"token: {reason}. Use a colour like '#0b1020', 'hsl(220 54% 8%)' or "
        "'oklch(62% 0.19 250)', a gradient of colours, or a font stack like "
        "\"'Inter', sans-serif\". The only CSS functions allowed are colour "
        "functions, gradients, calc() / min() / max() / clamp() and var(), "
        "because any other one can make every viewer's browser load a URL; no "
        f"braces, semicolons, comments or backslashes, max {MAX_THEME_VALUE_CHARS} characters."
    )


def unsafe_theme_value_problems(
    theme_config: object, extra_metadata: object, *, kind: str | None = None
) -> list[str]:
    """The palette values a REST write must refuse, one sentence each.

    The REST surface trusts its caller about what merely does nothing - a key
    the renderer does not read, a blank or non-string value - which it stores
    and no read ever forwards (see "Authoring guardrails" above). It does not
    trust it about what does harm: a non-empty string under a key the *kind*
    honours that the value guard refuses (a function that can make every
    viewer's browser load a URL, a character that escapes the declaration, a
    file id that is not a UUID) is refused before it is stored, in the base
    ``theme_config`` and in every ``extra_metadata.theme_variants`` palette,
    with the sentence the authoring tool and the import use. The read-time
    sanitizer drops such a value anyway; refusing it here tells the author
    instead of storing a colour no deck will ever draw.
    """
    from app.services.presentations import THEME_SCHEMES, THEME_VARIANTS_KEY, trim_theme_value

    palettes: list[tuple[str, object]] = [("theme_config", theme_config)]
    if isinstance(extra_metadata, dict):
        variants = extra_metadata.get(THEME_VARIANTS_KEY)
        if isinstance(variants, dict):
            palettes += [
                (f"extra_metadata.{THEME_VARIANTS_KEY}.{scheme}", variants[scheme])
                for scheme in THEME_SCHEMES
                if scheme in variants
            ]
    keys = theme_config_keys_for_kind(kind)
    problems: list[str] = []
    for label, palette in palettes:
        if not isinstance(palette, dict):
            continue
        for key, value in palette.items():
            if key not in keys or not isinstance(value, str) or not trim_theme_value(value):
                continue
            problem = _theme_value_refusal(key, value, label=label)
            if problem is not None:
                problems.append(problem)
    return problems


def validate_theme_variants(
    extra_metadata: object, *, kind: str | None = None
) -> tuple[object, list[str]]:
    """Validate ``extra_metadata.theme_variants`` the way ``theme_config`` is.

    Returns ``(extra_metadata, problems)`` — the metadata with each variant
    replaced by its accepted subset, and one line per rejected entry, prefixed
    with the variant it came from so the author can tell ``theme_variants.light``
    apart from the base palette. A variant is held to exactly the base's
    contract (:func:`validate_theme_config`, same *kind* whitelist): the
    renderer overlays it on the base at read time, so a key the base could not
    forward, a variant cannot either. Metadata without the slot is returned as
    is; anything that is not a JSON object is left alone here (the renderer's
    read-time sanitizer already ignores it) but reported.
    """
    from app.services.presentations import THEME_SCHEMES, THEME_VARIANTS_KEY

    if not isinstance(extra_metadata, dict) or THEME_VARIANTS_KEY not in extra_metadata:
        return extra_metadata, []
    variants = extra_metadata.get(THEME_VARIANTS_KEY)
    if not isinstance(variants, dict):
        return extra_metadata, [
            f"extra_metadata.{THEME_VARIANTS_KEY} must be an object mapping a "
            f"scheme ({' or '.join(THEME_SCHEMES)}) to a theme_config object, "
            f"got {type(variants).__name__}."
        ]
    problems: list[str] = []
    accepted_variants: dict[str, dict[str, str]] = {}
    for scheme, palette in variants.items():
        if scheme not in THEME_SCHEMES:
            problems.append(
                f"extra_metadata.{THEME_VARIANTS_KEY}.{scheme} is not a scheme the "
                f"renderer knows; use one of: {', '.join(THEME_SCHEMES)}."
            )
            continue
        accepted, palette_problems = validate_theme_config(palette, kind=kind)
        problems.extend(
            p.replace("theme_config", f"extra_metadata.{THEME_VARIANTS_KEY}.{scheme}", 1)
            for p in palette_problems
        )
        accepted_variants[scheme] = accepted
    return {**extra_metadata, THEME_VARIANTS_KEY: accepted_variants}, problems


def validate_supported_schemes(
    extra_metadata: object, *, kind: str | None = None
) -> tuple[object, list[str]]:
    """Validate the ``extra_metadata.supported_schemes`` declaration.

    Returns ``(extra_metadata, problems)``. On a presentation template the
    declaration, when present, must be a list naming at least one of the
    schemes the renderer knows (``dark`` / ``light``); it is stored in
    canonical order, de-duplicated. A declaration that names an unknown scheme
    or nothing at all is reported and the metadata returned as it was, so the
    caller refuses the write instead of storing a list
    :func:`~app.services.presentations.declared_supported_schemes` would then
    read as "declares nothing" - which would silently widen a Dark-only
    template back to both. A video template declares its schemes the same way.
    On any other kind the key is meaningless (a report skin dresses no deck)
    and is DROPPED without a problem: switching a template's kind must not make
    its metadata unsaveable.
    """
    from app.services.presentations import SUPPORTED_SCHEMES_KEY, THEME_SCHEMES

    if not isinstance(extra_metadata, dict) or SUPPORTED_SCHEMES_KEY not in extra_metadata:
        return extra_metadata, []
    if not has_color_schemes(kind):
        return {k: v for k, v in extra_metadata.items() if k != SUPPORTED_SCHEMES_KEY}, []
    raw = extra_metadata.get(SUPPORTED_SCHEMES_KEY)
    if not isinstance(raw, (list, tuple)):
        return extra_metadata, [
            f"extra_metadata.{SUPPORTED_SCHEMES_KEY} must be a list of the schemes "
            f"the template supports ({' / '.join(THEME_SCHEMES)}), got "
            f"{type(raw).__name__}."
        ]
    unknown = [
        repr(item) for item in raw
        if not isinstance(item, str) or item.strip().lower() not in THEME_SCHEMES
    ]
    if unknown:
        return extra_metadata, [
            f"extra_metadata.{SUPPORTED_SCHEMES_KEY} names a scheme the renderer "
            f"does not know: {', '.join(unknown)}; use only {' / '.join(THEME_SCHEMES)}."
        ]
    declared = {item.strip().lower() for item in raw if isinstance(item, str)}
    ordered = [scheme for scheme in THEME_SCHEMES if scheme in declared]
    if not ordered:
        return extra_metadata, [
            f"extra_metadata.{SUPPORTED_SCHEMES_KEY} must name at least one scheme "
            f"({' / '.join(THEME_SCHEMES)}): a template that supports no theme "
            "cannot dress any deck."
        ]
    return {**extra_metadata, SUPPORTED_SCHEMES_KEY: ordered}, []


def validate_slide_cap(
    extra_metadata: object, *, kind: str | None = None
) -> tuple[object, list[str]]:
    """Validate the ``extra_metadata.max_slides`` declaration.

    Returns ``(extra_metadata, problems)`` - the same contract as
    :func:`validate_supported_schemes`. On a presentation template the
    declaration, when present, must be a whole number from 1 to
    ``MAX_SLIDE_CAP`` (a numeric string is accepted and stored as the int);
    anything else - a word, a bool, zero, a number past the ceiling - is
    reported and the metadata returned as it was, so the caller refuses the
    write instead of storing a value
    :func:`~app.services.presentations.template_slide_cap` would silently read
    as the default (or clamp). A ``null`` DROPS the key: that is how "the
    platform default" is stored. On any other kind the key is meaningless (a
    report has no slides) and is dropped without a problem.
    """
    from app.services.presentation_spec import DEFAULT_SLIDE_CAP, MAX_SLIDE_CAP
    from app.services.presentations import SLIDE_CAP_KEY

    if not isinstance(extra_metadata, dict) or SLIDE_CAP_KEY not in extra_metadata:
        return extra_metadata, []
    without = {k: v for k, v in extra_metadata.items() if k != SLIDE_CAP_KEY}
    if not is_presentation_kind(kind):
        return without, []
    raw = extra_metadata.get(SLIDE_CAP_KEY)
    if raw is None:
        return without, []
    value: int | None = None
    if isinstance(raw, bool):
        value = None
    elif isinstance(raw, int):
        value = raw
    elif isinstance(raw, float) and raw.is_integer():
        value = int(raw)
    elif isinstance(raw, str) and raw.strip().isdigit():
        value = int(raw.strip())
    if value is None:
        return extra_metadata, [
            f"extra_metadata.{SLIDE_CAP_KEY} must be a whole number of slides from 1 to "
            f"{MAX_SLIDE_CAP} (the platform default is {DEFAULT_SLIDE_CAP}), got {raw!r}."
        ]
    if value < 1 or value > MAX_SLIDE_CAP:
        return extra_metadata, [
            f"extra_metadata.{SLIDE_CAP_KEY} is {value}; a deck holds from 1 to "
            f"{MAX_SLIDE_CAP} slides ({MAX_SLIDE_CAP} is the platform ceiling no template can raise)."
        ]
    return {**extra_metadata, SLIDE_CAP_KEY: value}, []


def validate_template_heroes(
    extra_metadata: object, *, kind: str | None = None
) -> tuple[object, list[str]]:
    """Validate the ``extra_metadata.heroes`` declaration: a template's own covers.

    Returns ``(extra_metadata, problems)`` - the same contract as
    :func:`validate_supported_schemes`. On a presentation template the
    declaration, when present, must be a list of template-only covers
    (``TEMPLATE_HEROES``); it is stored in catalog order, de-duplicated. A
    name outside them is reported and the metadata returned as it was, so the
    caller refuses the write instead of storing a cover
    :func:`~app.services.presentations.template_heroes` would silently ignore.
    A ``null`` or an empty list DROPS the key: the template then offers the
    shared covers only. On any other kind the key is dropped without a problem.
    """
    from app.services.presentation_spec import SHARED_HEROES, TEMPLATE_HEROES
    from app.services.presentations import TEMPLATE_HEROES_KEY

    if not isinstance(extra_metadata, dict) or TEMPLATE_HEROES_KEY not in extra_metadata:
        return extra_metadata, []
    without = {k: v for k, v in extra_metadata.items() if k != TEMPLATE_HEROES_KEY}
    if not is_presentation_kind(kind):
        return without, []
    raw = extra_metadata.get(TEMPLATE_HEROES_KEY)
    if raw is None:
        return without, []
    if not isinstance(raw, (list, tuple)):
        return extra_metadata, [
            f"extra_metadata.{TEMPLATE_HEROES_KEY} must be a list of the covers of the "
            f"template's own ({' / '.join(TEMPLATE_HEROES)}), got {type(raw).__name__}."
        ]
    unknown = [
        repr(item) for item in raw if not isinstance(item, str) or item.strip().lower() not in TEMPLATE_HEROES
    ]
    if unknown:
        return extra_metadata, [
            f"extra_metadata.{TEMPLATE_HEROES_KEY} names {', '.join(unknown)}: list only the covers "
            f"of the template's own ({' / '.join(TEMPLATE_HEROES)}); every deck already "
            f"offers {' / '.join(SHARED_HEROES)}."
        ]
    declared = {item.strip().lower() for item in raw}
    ordered = [hero for hero in TEMPLATE_HEROES if hero in declared]
    return ({**extra_metadata, TEMPLATE_HEROES_KEY: ordered} if ordered else without), []


def validate_chrome(
    extra_metadata: object, *, kind: str | None = None
) -> tuple[object, list[str]]:
    """Validate the ``extra_metadata.chrome`` header / footer declaration.

    Returns ``(extra_metadata, problems)`` - the same contract as
    :func:`validate_supported_schemes`. On a presentation template the
    declaration is normalised to its canonical shape
    (:func:`~app.services.presentation_chrome.normalize_template_chrome`):
    a pattern naming an undeclared placeholder, a variable with a bad key or
    no label, a declared variable no pattern uses, or too many of them is
    reported and the metadata returned as it was, so the caller refuses the
    write instead of storing a footer that would read ``{{classifcation}}`` on
    every slide. A declaration that says nothing (``null``, ``{}``, blank
    patterns) DROPS the key: that is how "no pattern, v1 footer" is stored. On
    any other kind the key is meaningless (a report has no slide footer) and
    is dropped without a problem.
    """
    from app.services.presentation_chrome import CHROME_KEY, normalize_template_chrome

    if not isinstance(extra_metadata, dict) or CHROME_KEY not in extra_metadata:
        return extra_metadata, []
    without = {k: v for k, v in extra_metadata.items() if k != CHROME_KEY}
    if not is_presentation_kind(kind):
        return without, []
    canonical, problems = normalize_template_chrome(extra_metadata.get(CHROME_KEY))
    if problems:
        return extra_metadata, problems
    if canonical is None:
        return without, []
    return {**extra_metadata, CHROME_KEY: canonical}, []


def validate_video_style(
    extra_metadata: object, *, kind: str | None = None
) -> tuple[object, list[str]]:
    """Validate the ``extra_metadata.video`` rules of a video template.

    Returns ``(extra_metadata, problems)`` - the same contract as
    :func:`validate_chrome`. On a video template the rules are held to
    :class:`~app.services.video_studio.templates.VideoTemplateStyle` (every
    preset must make a valid video, every lock must name a preset) and stored
    canonical; an empty value DROPS the key (a template that presets nothing,
    its palette alone). On any other kind the key is meaningless and is dropped
    without a problem.
    """
    from app.services.video_studio.templates import VIDEO_STYLE_KEY, validate_style

    if not isinstance(extra_metadata, dict) or VIDEO_STYLE_KEY not in extra_metadata:
        return extra_metadata, []
    without = {k: v for k, v in extra_metadata.items() if k != VIDEO_STYLE_KEY}
    if (kind or "").strip().lower() != VIDEO_KIND:
        return without, []
    canonical, problems = validate_style(extra_metadata.get(VIDEO_STYLE_KEY))
    if problems:
        return extra_metadata, problems
    if canonical is None:
        return without, []
    return {**extra_metadata, VIDEO_STYLE_KEY: canonical}, []


def validate_layouts(
    raw: object,
    *,
    kind: str,
) -> tuple[list[dict], list[str]]:
    """Normalize a proposed ``layouts`` catalog and report what is unusable.

    Returns ``(layouts, problems)``. For ``kind = "presentation"`` every entry
    must name a layout id that really exists in ``SLIDE_LAYOUTS``, and for
    ``kind = "report"`` a block id in ``REPORT_BLOCKS``. Either way the
    entry's ``fields`` list is (re)derived from the Pydantic models via
    ``layout_catalog()`` / ``block_catalog()`` — same reasoning as the built-in
    seed: a container must not be able to advertise a field the spec validator
    would then reject.

    A RETIRED v1 slide layout is dropped silently rather than reported:
    a template row that predates the retirement still carries the four ids,
    and there is nothing left to advertise under them - the slides that wore
    them are upgraded to the current catalog on read. Same spirit as the
    ``filigran`` theme alias: a stored value the platform outgrew is read as
    what it means today, never as an author's mistake. An id the catalog
    never held stays a problem.
    """
    from app.services.presentation_legacy import is_retired_layout
    from app.services.presentation_spec import SLIDE_LAYOUTS, layout_catalog
    from app.services.report_spec import REPORT_BLOCKS, block_catalog

    if raw is None:
        return [], []
    normalized_kind = (kind or "").strip().lower()
    if normalized_kind == VIDEO_KIND:
        # A video has scenes, not layouts: whatever a kind switch carries over
        # is dropped, as a deck-only key is on any other kind.
        return [], []
    if not isinstance(raw, list):
        return [], [
            "layouts must be a JSON array of layout objects, got "
            f"{type(raw).__name__}."
        ]

    # ``known`` empty means "this kind has no renderer contract yet" — the ids
    # stay free-form and no ``fields`` list is derived. The consequence clause
    # spells out what an invented id actually costs, per kind: "invalid" alone
    # tells an author nothing about why it matters.
    if normalized_kind == PRESENTATION_KIND:
        catalog, known = layout_catalog(), SLIDE_LAYOUTS
        noun = "slide layout"
        consequence = "the validator refuses every slide built with it"
    elif normalized_kind == REPORT_KIND:
        catalog, known = block_catalog(), REPORT_BLOCKS
        noun = "report block"
        consequence = "a report built from it renders nothing for that section"
    else:
        catalog, known = {}, ()
        noun = consequence = ""
    layouts: list[dict] = []
    problems: list[str] = []
    seen: set[str] = set()

    for idx, entry in enumerate(raw):
        if isinstance(entry, str):
            entry = {"id": entry}
        if not isinstance(entry, dict):
            problems.append(
                f"layouts[{idx}] must be an object with an 'id' (or the layout "
                f"id as a bare string), got {type(entry).__name__}."
            )
            continue
        layout_id = str(entry.get("id") or "").strip()
        if not layout_id:
            problems.append(f"layouts[{idx}] has no 'id'.")
            continue
        if normalized_kind == PRESENTATION_KIND and is_retired_layout(layout_id):
            continue
        if known and layout_id not in known:
            problems.append(
                f"layouts[{idx}]: '{layout_id}' is not a {noun}. Every layout a "
                f"{normalized_kind} template advertises must exist, or "
                f"{consequence}. Pick from: {', '.join(known)}."
            )
            continue
        if layout_id in seen:
            problems.append(f"layouts[{idx}]: '{layout_id}' is listed twice.")
            continue
        seen.add(layout_id)

        normalized = {k: v for k, v in entry.items() if k != "id"}
        normalized["id"] = layout_id
        if catalog:
            # Derived, never taken from the caller — see ``_build_layouts`` in
            # ``template_container_seed`` for the same rule on the seeded one.
            normalized["fields"] = catalog[layout_id]
        layouts.append(normalized)

    return layouts, problems


def coerce_metadata_to_kind(data: dict, *, kind: str) -> tuple[dict, list[str]]:
    """Reduce a container's metadata to what *kind*'s renderer honours.

    Returns ``(data, dropped)``: a copy of *data* with ``kind`` set and its
    kind-dependent fields (``theme_config``, ``layouts``,
    ``extra_metadata.theme_variants`` / ``supported_schemes``) replaced by the
    subset the validators of *kind* accept, plus one line per entry that was
    left out, in the validators' own words. The result re-validates clean, so
    the strict create pipeline stores it as is.

    This is the conversion step behind duplicating a row of a retired kind
    (``asset-library``, an older ``dashboard``) as a presentation template.
    Such a row's metadata was stored for a renderer that no longer exists, or
    never did, so holding it to the presentation contract on the way in - the
    way :func:`validate_theme_config` and friends hold an import - would refuse
    the copy over a palette key or a layout id its new kind cannot use, on a
    source the user cannot fix (read-only, or of a kind that has no editor
    left). A row of a SUPPORTED kind never comes through here: a problem in a
    row of the copy's own kind is a real problem in the source, and the strict
    pipeline names it.
    """
    from app.services.presentations import (
        SLIDE_CAP_KEY,
        SUPPORTED_SCHEMES_KEY,
        TEMPLATE_HEROES_KEY,
        THEME_VARIANTS_KEY,
    )

    accepted_config, dropped = validate_theme_config(data.get("theme_config"), kind=kind)
    layouts, layout_problems = validate_layouts(data.get("layouts"), kind=kind)
    dropped.extend(layout_problems)

    extra_metadata = data.get("extra_metadata")
    if extra_metadata is None:
        extra_metadata = {}
    elif isinstance(extra_metadata, dict):
        extra_metadata = dict(extra_metadata)
    else:
        # Not an object at all: nothing in it can be kept, and losing the whole
        # value is an omission like any other, so it is recorded before the
        # substitution rather than vanishing behind an empty ``dropped``.
        dropped.append(
            "extra_metadata must be a JSON object mapping keys to values, got "
            f"{type(extra_metadata).__name__}; the value was not carried over."
        )
        extra_metadata = {}
    extra_metadata, variant_problems = validate_theme_variants(extra_metadata, kind=kind)
    dropped.extend(variant_problems)
    if variant_problems and not isinstance(extra_metadata.get(THEME_VARIANTS_KEY), dict):
        # A slot that is not even an object is returned as it was; the renderer
        # ignores it, but the strict pipeline would refuse it, so it goes.
        extra_metadata = {k: v for k, v in extra_metadata.items() if k != THEME_VARIANTS_KEY}
    extra_metadata, scheme_problems = validate_supported_schemes(extra_metadata, kind=kind)
    dropped.extend(scheme_problems)
    if scheme_problems:
        # An unusable declaration is returned as it was so a write is refused;
        # here it is dropped, which reads as "declares nothing" - both schemes.
        extra_metadata = {k: v for k, v in extra_metadata.items() if k != SUPPORTED_SCHEMES_KEY}
    extra_metadata, cap_problems = validate_slide_cap(extra_metadata, kind=kind)
    dropped.extend(cap_problems)
    if cap_problems:
        # Same rule: an unreadable cap is dropped, which reads as the
        # platform default, and the omission is on the list.
        extra_metadata = {k: v for k, v in extra_metadata.items() if k != SLIDE_CAP_KEY}
    extra_metadata, hero_problems = validate_template_heroes(extra_metadata, kind=kind)
    dropped.extend(hero_problems)
    if hero_problems:
        # Same rule: an unreadable declaration is dropped, which reads
        # as the shared covers only.
        extra_metadata = {k: v for k, v in extra_metadata.items() if k != TEMPLATE_HEROES_KEY}
    extra_metadata, style_problems = validate_video_style(extra_metadata, kind=kind)
    dropped.extend(style_problems)
    if style_problems:
        from app.services.video_studio.templates import VIDEO_STYLE_KEY

        extra_metadata = {k: v for k, v in extra_metadata.items() if k != VIDEO_STYLE_KEY}

    return (
        {
            **data,
            "kind": kind,
            "theme_config": accepted_config,
            "layouts": layouts,
            "extra_metadata": extra_metadata,
        },
        dropped,
    )


def is_builtin_container(container: TemplateContainer) -> bool:
    """True when *container* is code-managed and full-synced on every startup.

    A built-in is not a special row type — it is a company-managed container
    carrying the ``builtin`` tag (``template_container_seed``). Editing one is
    pointless rather than forbidden: the next deploy overwrites every field from
    the seed, so the write surfaces refuse it and tell the caller to duplicate.
    """
    from app.services.template_container_seed import BUILTIN_TAG

    return bool(container.is_company_managed) and BUILTIN_TAG in (
        container.tags or []
    )


#: Where a built-in's description stops describing the DESIGN and starts
#: describing its own read-only status (``template_container_seed``).
_BUILTIN_STATUS_SENTENCE = re.compile(r"\s*Read-only:.*\Z", re.DOTALL)


def derived_description(source_name: str, description: str | None, *, from_builtin: bool) -> str | None:
    """The description a copy of *source_name* carries.

    A built-in's description ends with a sentence about ITSELF ("Read-only: it
    is kept in sync with the platform on every startup - duplicate it to derive
    your own palette"). Copied verbatim onto the duplicate it is false twice
    over - the copy is editable, and it IS the duplicate the sentence tells the
    reader to make - so that sentence is replaced by what is true of the copy:
    where it came from, and that it is the caller's to edit. A copy of an
    ordinary template keeps the description as written.
    """
    if not from_builtin:
        return description
    design = _BUILTIN_STATUS_SENTENCE.sub("", description or "").strip()
    origin = f"Derived from the built-in '{source_name}' template; edit it freely."
    return f"{design} {origin}" if design else origin


def is_scheme_filter(theme: str | None) -> bool:
    """Whether a ``theme`` filter names a colour scheme (``dark`` / ``light``).

    A scheme is a property DERIVED from the palette, so it cannot be pushed to
    SQL: :func:`apply_structured_filters` leaves it out and the caller applies
    :func:`filter_by_theme` to the rows. Any other value is the legacy exact
    label match, which SQL handles.
    """
    from app.services.presentations import THEME_SCHEMES

    return bool(theme) and theme.strip().lower() in THEME_SCHEMES


def theme_filter_matches(container, theme: str | None) -> bool:
    """Whether *container* passes the ``theme`` filter.

    The filter takes one of two forms, decided by :func:`is_scheme_filter`:

    * A colour scheme (``dark`` / ``light``) uses supported-scheme semantics
      ONLY: it keeps the presentation templates able to dress a deck in that
      scheme (:func:`container_supported_schemes`), so a search reads exactly
      the list the deck editor restricts its theme picker to. The legacy label
      is never consulted for a scheme: a light-only template that
      still carries the label ``dark`` cannot dress a dark deck, so it must not
      come back for ``theme=dark``. Two consequences follow from the supported
      list (the declaration first, the palettes as fallback): a
      container of a non-presentation kind (a report skin, a legacy asset
      library) has no supported scheme and is never returned for a scheme,
      whatever its label says; a presentation template that declares nothing
      and has no readable palette (no ``theme_config`` background) dresses
      either scheme in the renderer's own colours and is returned for both.
    * Any other value is matched exactly (case-insensitively) against the
      legacy free-text ``theme`` label, so an instruction written against that
      label keeps working.

    No filter passes everything.
    """
    if not theme or not theme.strip():
        return True
    wanted = theme.strip().lower()
    if is_scheme_filter(wanted):
        return wanted in container_supported_schemes(container)
    label = getattr(container, "theme", None)
    return isinstance(label, str) and label.strip().lower() == wanted


def filter_by_theme(containers, theme: str | None) -> list:
    """The rows of *containers* that pass the ``theme`` filter (see :func:`theme_filter_matches`)."""
    return [c for c in containers if theme_filter_matches(c, theme)]


def apply_structured_filters(
    query,
    *,
    name: str | None = None,
    kind: str | None = None,
    tags: list[str] | None = None,
    theme: str | None = None,
    aspect_ratio: str | None = None,
    match_all_tags: bool = False,
):
    """Narrow *query* with the structured template filters.

    Match semantics, chosen so an agent's filter behaves predictably:

    * ``name`` — case-insensitive substring (an agent rarely knows the exact
      stored label, and this is the one field a human names loosely).
    * ``kind`` / ``aspect_ratio`` — case-insensitive **exact** match. These are
      short controlled labels; substring matching there would make ``"4:3"``
      match ``"14:3"``-style values and quietly widen results.
    * ``theme`` — a colour scheme (``dark`` / ``light``): the presentation
      templates that can dress a deck in it. A scheme is derived from
      the palette, so it is NOT applied here - :func:`is_scheme_filter` tells
      the caller to run :func:`filter_by_theme` over the rows instead, and the
      legacy label plays no part in that decision. Any other value is
      the legacy exact match on the free-text label, applied in SQL like the
      other controlled labels. :func:`theme_filter_matches` states the whole
      contract; never re-spell it in a route or a tool.
    * ``tags`` — case-insensitive membership. ``match_all_tags=False`` (default)
      is OR (any tag matches), ``True`` is AND (every tag must be present).

    ``tags`` is a JSON column, so membership is tested by casting to text and
    matching the quoted, lower-cased token (``"dark"``). Quoting the token is
    what keeps the test from matching a *substring* of a neighbouring tag
    (bare ``dark`` would also match a ``darkroom`` tag).
    """
    if name:
        query = query.where(TemplateContainer.name.ilike(f"%{name.strip()}%"))
    if kind:
        query = query.where(func.lower(TemplateContainer.kind) == kind.strip().lower())
    if theme and not is_scheme_filter(theme):
        query = query.where(
            func.lower(TemplateContainer.theme) == theme.strip().lower()
        )
    if aspect_ratio:
        query = query.where(
            func.lower(TemplateContainer.aspect_ratio) == aspect_ratio.strip().lower()
        )

    cleaned_tags = [t.strip().lower() for t in (tags or []) if t and t.strip()]
    if cleaned_tags:
        clauses = [
            func.lower(cast(TemplateContainer.tags, Text)).contains(f'"{tag}"')
            for tag in cleaned_tags
        ]
        query = query.where(*clauses) if match_all_tags else query.where(or_(*clauses))
    return query


async def search_containers(
    db: AsyncSession,
    *,
    user_id: uuid.UUID | None,
    user_group_ids: list[str] | None = None,
    name: str | None = None,
    kind: str | None = None,
    tags: list[str] | None = None,
    theme: str | None = None,
    aspect_ratio: str | None = None,
    match_all_tags: bool = False,
    limit: int = MAX_SEARCH_RESULTS,
) -> list[TemplateContainer]:
    """Structured search over the containers visible to *user_id*.

    Visibility is ``caller_visible_filter``: the standard three-tier model (own
    + group-shared + company-managed) for a signed-in caller. A caller with no
    account (``user_id=None`` - e.g. an agent answering on an external channel
    with no linked account) sees the company-wide tier alone, company-managed
    containers not narrowed by ``access_restricted`` (the run of a
    company-managed assignment with no user keeps every company-managed one);
    it has no owner and no group memberships, so it must never inherit another
    user's private templates.

    There is deliberately **no** ``is_admin`` parameter. This function backs the
    agent-facing ``search_templates`` tool, where an admin-wide bypass would let
    an admin's agent surface other users' private templates in a chat reply. A
    future admin-scoped search must add that lever explicitly (and justify it)
    rather than find it already wired and defaulted off — see
    ``build_template_search_tools``.

    No vector search is performed — see the module docstring.
    """
    from app.api.v1.helpers import caller_visible_filter

    query = select(TemplateContainer).where(
        caller_visible_filter(TemplateContainer, user_id, user_group_ids)
    )

    query = apply_structured_filters(
        query,
        name=name,
        kind=kind,
        tags=tags,
        theme=theme,
        aspect_ratio=aspect_ratio,
        match_all_tags=match_all_tags,
    )
    # Stable, useful ordering: most-used first, then newest, so an agent's
    # truncated result set is the most relevant slice rather than an arbitrary one.
    query = query.order_by(
        TemplateContainer.lookup_count.desc(), TemplateContainer.created_at.desc()
    )
    bound = max(1, min(limit, MAX_SEARCH_RESULTS))
    if is_scheme_filter(theme):
        # A colour scheme is derived from the palette, not stored, so the rows
        # are filtered in Python: fetch the ordered candidates and cut
        # to the bound AFTER the filter, or a page of light-only templates
        # would leave a ``theme="dark"`` search empty while dark ones exist.
        result = await db.execute(query)
        return filter_by_theme(list(result.scalars().all()), theme)[:bound]

    result = await db.execute(query.limit(bound))
    return list(result.scalars().all())


async def record_lookups(db: AsyncSession, containers: list[TemplateContainer]) -> None:
    """Bump ``lookup_count`` for every container an agent actually retrieved.

    Best-effort analytics (the "template search hit rate"
    metric): a failure here must never fail the caller's search, so the exception
    is logged and swallowed. The caller owns the commit.
    """
    if not containers:
        return
    try:
        for container in containers:
            container.lookup_count = (container.lookup_count or 0) + 1
    except Exception as exc:  # pragma: no cover - defensive
        from app.core.utils import format_exception_message

        logger.warning(
            "Failed to record template lookup counts: %s",
            format_exception_message(exc),
        )
