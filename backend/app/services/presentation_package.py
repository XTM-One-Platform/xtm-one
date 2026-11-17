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

"""Portable presentation packages — export a deck as JSON, import it elsewhere.

A deck is a validated :class:`~app.services.presentation_spec.PresentationSpec`
plus two things that only mean something on the install it was made on: the
id of the template container that styles it, and the platform file ids its
image / logo blocks point at. Copying the spec alone therefore produces a deck
with the wrong palette and broken pictures. The package carries what is needed
to rebuild both on the other side:

* the **template by slug and name** — a built-in template (the Filigran brand
  decks) is found again through its seeded ``slug:<slug>`` tag, an authored one
  through its name among the importer's visible containers, and when neither
  matches the deck falls back to the install's default template, reported as
  such rather than silently;
* every **referenced file**, as ``filename`` + ``sha256`` + the template it
  belongs to (so a seeded brand asset is *matched* to the same asset on the
  target install instead of duplicated) **and** its bytes, base64-encoded, as
  the fallback for anything the target install does not already hold — a
  generated image, an ad-hoc upload, an asset of a template that was never
  shipped there.

Import never trusts the bundle's ids: every file reference is rewritten through
:func:`~app.services.template_package.remap_asset_ids` to the id resolved or
created locally, then the spec goes through :func:`~app.services.presentations.save_presentation`
exactly like an agent-authored deck (validation, file-reference verification
against the importer, first ``PresentationVersion`` tagged ``import``). An
unresolvable reference fails the import with the filenames named, because
dropping a reference would change the deck without anyone noticing.

Parsing an uploaded package is parsing hostile input: the JSON is bounded on
size, the asset list on count, every embedded blob on decoded size, and every
filename is reduced to a safe basename before it can reach storage or a
``Content-Disposition`` header.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import logging
import posixpath
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.utils import format_exception_message
from app.models.file_attachment import FileAttachment
from app.models.presentation import VERSION_SOURCE_IMPORT, Presentation
from app.models.template_container import TEMPLATE_ASSET_SOURCE, TemplateContainer
from app.services.file_storage import MAX_UPLOAD_SIZE_BYTES, guess_content_type

logger = logging.getLogger(__name__)

#: Bumped when the envelope shape changes incompatibly. A reader refuses a
#: version it does not know rather than guessing at the fields.
PACKAGE_VERSION = 1
#: Distinguishes a presentation package from a template package (both are
#: JSON; ``export_type`` is what tells the two importers apart).
PACKAGE_EXPORT_TYPE = "presentation"

#: Upper bound on the whole uploaded document. Embedded assets dominate the
#: size; a deck of a few dozen slides with a handful of screenshots is a few MB.
MAX_PACKAGE_BYTES = 64 * 1024 * 1024
#: Entries in ``assets`` — a deck cannot legitimately reference more files than
#: it has blocks, and a spec is bounded at ``MAX_SLIDE_CAP`` slides.
MAX_PACKAGE_ASSETS = 400
#: One decoded embedded blob. Same ceiling as a direct upload — the package is
#: not a way around it.
MAX_EMBEDDED_ASSET_BYTES = MAX_UPLOAD_SIZE_BYTES
#: Namesakes of ONE packaged file the import may read to find the one holding
#: its bytes, once no digest the rows record has settled it. They share a
#: filename inside one template, so there are rarely more than two or three;
#: past the bound the rest are not compared and the name rule decides.
MAX_CONTENT_MATCH_READS = 8
#: Bytes ONE import may read, over the whole package, to compare namesakes by
#: content. A package lists up to ``MAX_PACKAGE_ASSETS`` files, so without an
#: import-wide ceiling a template holding a few large namesakes would turn one
#: upload into gigabytes of object-store reads.
MAX_CONTENT_MATCH_READ_BYTES = 64 * 1024 * 1024
#: Objects ONE import may download to compare namesakes, over the whole package.
#: The two bounds above cap one file's reads and all reads' bytes, not their
#: number: a package naming hundreds of ambiguous files, in as many templates,
#: could otherwise fetch eight small objects for each of them.
MAX_CONTENT_MATCH_DOWNLOADS = 64

#: ``FileAttachment.source`` for a file created from an embedded blob. Its own
#: value (not ``chat`` or ``template_asset``) so the Files browser classifies it
#: honestly and a later cleanup can find every file an import brought in.
PRESENTATION_IMPORT_SOURCE = "presentation_import"

_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._ ()\[\]-]+")
_MAX_FILENAME_CHARS = 200


class PresentationPackageError(ValueError):
    """The bundle is not a presentation package this reader accepts.

    The message is written for the person who uploaded it — it names the field
    or the file at fault, never an internal path.
    """


@dataclass(slots=True)
class PackagedAsset:
    """One file a packaged deck references, as carried by the bundle."""

    original_id: str
    filename: str
    content_type: str | None = None
    #: Lower-case hex SHA-256 of the packaged file - the declared digest (checked
    #: against the bytes when they are embedded), else that of the embedded
    #: bytes, ``None`` for a reference carrying neither. What tells apart the
    #: namesakes of the destination's template.
    sha256: str | None = None
    asset_role: str | None = None
    description: str | None = None
    template_slug: str | None = None
    template_name: str | None = None
    #: Decoded bytes when the bundle embedded them, else ``None`` (the import
    #: then has to find the asset on the target install or refuse).
    data: bytes | None = None
    #: The exporting side already found no file behind this reference.
    missing: bool = False


@dataclass(slots=True)
class PresentationPackage:
    """A parsed bundle, before anything is resolved against the database."""

    spec: dict[str, Any]
    template_slug: str | None = None
    template_name: str | None = None
    assets: list[PackagedAsset] = field(default_factory=list)
    #: Where the bundle came from — shown in the version summary so a deck's
    #: history says which install it arrived from.
    source_platform: str | None = None


@dataclass(slots=True)
class ImportReport:
    """What the import did — returned with the created deck so the UI can say
    "3 assets matched, 2 copied, styled with the default template" instead of a
    bare success."""

    assets_total: int = 0
    #: References re-pointed at an asset the target install already held
    #: (a seeded brand asset, an asset of an identically named template).
    assets_matched: int = 0
    #: References served from the bundle's embedded bytes (new files, owned by
    #: the importer).
    assets_copied: int = 0
    #: ``matched`` (the packaged template exists here), ``default`` (fell back
    #: to the install's default template), ``none`` (no template at all).
    template: str = "none"
    template_name: str | None = None
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "assets_total": self.assets_total,
            "assets_matched": self.assets_matched,
            "assets_copied": self.assets_copied,
            "template": self.template,
            "template_name": self.template_name,
            "warnings": list(self.warnings),
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def safe_filename(name: object, fallback: str = "asset") -> str:
    """Reduce a bundle-supplied filename to a storage-safe basename.

    Strips any directory part (``../`` and absolute paths included), replaces
    every character outside a conservative allow-list, and bounds the length so
    the value fits the ``FileAttachment.filename`` column and cannot break a
    download header. Never returns an empty string.
    """
    raw = str(name or "").replace("\\", "/")
    base = posixpath.basename(raw).strip()
    base = _SAFE_FILENAME_RE.sub("_", base).strip(" ._")
    if not base or base in {".", ".."}:
        base = fallback
    if len(base) > _MAX_FILENAME_CHARS:
        stem, dot, ext = base.rpartition(".")
        if dot and len(ext) <= 12:
            base = stem[: _MAX_FILENAME_CHARS - len(ext) - 1] + "." + ext
        else:
            base = base[:_MAX_FILENAME_CHARS]
    return base


def _template_slug(container: TemplateContainer | None) -> str | None:
    """The seeded slug of a built-in container (``slug:<slug>`` tag), else ``None``."""
    if container is None:
        return None
    from app.services.template_container_seed import SLUG_TAG_PREFIX

    for tag in container.tags or []:
        if isinstance(tag, str) and tag.startswith(SLUG_TAG_PREFIX):
            slug = tag[len(SLUG_TAG_PREFIX) :].strip()
            if slug:
                return slug
    return None


def _optional_str(value: object, *, max_chars: int, what: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise PresentationPackageError(f"{what} must be a string.")
    value = value.strip()
    if not value:
        return None
    if len(value) > max_chars:
        raise PresentationPackageError(f"{what} is too long (max {max_chars} characters).")
    return value


_SHA256_HEX_RE = re.compile(r"[0-9a-f]{64}")


def _canonical_digest(value: object) -> str | None:
    """*value* as a lower-case hex SHA-256, or ``None`` when it is not one."""
    if not isinstance(value, str):
        return None
    digest = value.strip().lower()
    return digest if _SHA256_HEX_RE.fullmatch(digest) else None


def _optional_digest(value: object, *, what: str) -> str | None:
    """A declared SHA-256, lower-cased; ``None`` when absent, refused when not a digest.

    A reference without bytes is verified against nothing, so a value that is
    not a digest must not be taken at its word by the namesake comparison.
    """
    text = _optional_str(value, max_chars=64, what=what)
    if text is None:
        return None
    digest = _canonical_digest(text)
    if digest is None:
        raise PresentationPackageError(f"{what} must be a SHA-256 digest (64 hexadecimal characters).")
    return digest


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _walk_file_reference_ids(spec: dict[str, Any]) -> list[str]:
    """Every file id the spec references, in document order, deduplicated."""
    from app.services.presentation_export import referenced_file_ids

    ordered: list[str] = []
    seen: set[str] = set()

    def visit(node: object) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "file_id" or key.endswith("_file_id"):
                    if isinstance(value, str) and value and value not in seen:
                        seen.add(value)
                        ordered.append(value)
                else:
                    visit(value)
        elif isinstance(node, list):
            for item in node:
                visit(item)

    visit(spec)
    # ``referenced_file_ids`` is the render-time authority on what counts as a
    # reference; anything it knows that the walk above missed is appended so
    # the two can never disagree on which files travel with the deck.
    for file_id in sorted(referenced_file_ids(spec, None)):
        if file_id not in seen:
            seen.add(file_id)
            ordered.append(file_id)
    return ordered


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


async def build_presentation_package(
    db: AsyncSession,
    presentation: Presentation,
    *,
    user,
    platform_name: str | None = None,
) -> dict[str, Any]:
    """The JSON-safe bundle for *presentation*, assets embedded.

    *user* is the exporter: a referenced file the exporter may not download
    (:func:`~app.services.file_access.authorize_file_access` says no) travels
    as a reference only — filename, hash, owning template — never as bytes. The
    package therefore carries exactly what the exporter could already fetch
    picture by picture through the download route, never more. Reading a deck grants the pictures its slides show, so a
    reader's package embeds them; the deck is passed as the ``presentation_id`` hint, and the
    grant resolves it by primary key instead of scanning the reader's decks
    once per picture. Reference-only is what remains for a file no path admits
    (on a validated spec, none of the deck's own pictures) and for a lost
    object. The import side then matches a reference by template + filename or
    names it as missing.
    """
    from app.services.file_access import authorize_file_access
    from app.services.file_storage import file_storage

    spec = dict(presentation.spec or {})
    file_ids = _walk_file_reference_ids(spec)

    container: TemplateContainer | None = None
    if presentation.template_container_id is not None:
        container = await db.get(TemplateContainer, presentation.template_container_id)

    rows: dict[str, FileAttachment] = {}
    parsed_ids: list[uuid.UUID] = []
    for raw in file_ids:
        try:
            parsed_ids.append(uuid.UUID(raw))
        except (TypeError, ValueError):
            continue
    if parsed_ids:
        result = await db.execute(select(FileAttachment).where(FileAttachment.id.in_(parsed_ids)))
        rows = {str(row.id): row for row in result.scalars().all()}

    containers: dict[uuid.UUID, TemplateContainer] = {}
    container_ids = {row.template_container_id for row in rows.values() if row.template_container_id is not None}
    if container_ids:
        result = await db.execute(select(TemplateContainer).where(TemplateContainer.id.in_(container_ids)))
        containers = {c.id: c for c in result.scalars().all()}

    assets: list[dict[str, Any]] = []
    for file_id in file_ids:
        row = rows.get(file_id)
        if row is None:
            # A dangling reference is exported as-is: the import reports it as
            # missing instead of this side silently editing the deck.
            assets.append({"id": file_id, "filename": None, "missing": True})
            continue
        owning = containers.get(row.template_container_id) if row.template_container_id else None
        entry: dict[str, Any] = {
            "id": str(row.id),
            "filename": row.filename,
            "content_type": row.content_type,
            "size": int(row.size or 0),
            "sha256": None,
            "asset_role": row.asset_role,
            "description": row.description,
            "template": ({"slug": _template_slug(owning), "name": owning.name} if owning is not None else None),
            "data_base64": None,
        }
        try:
            # This caller fetches every picture of ONE deck: the deck is the
            # hint, so the deck-reference grant resolves it by primary
            # key instead of scanning the exporter's decks once per picture.
            await authorize_file_access(row, user, db, presentation_id=presentation.id)
        except HTTPException:
            # Reference only: the exporter may not read these bytes, so the
            # package must not carry them either.
            assets.append(entry)
            continue
        try:
            data = await file_storage.download(row.s3_object_name)
        except Exception as exc:  # a lost object must not kill the export
            logger.warning(
                "Presentation export: could not read file %s (%s); exported as reference only",
                row.id,
                exc,
            )
        else:
            entry["size"] = len(data)
            entry["sha256"] = hashlib.sha256(data).hexdigest()
            entry["data_base64"] = base64.b64encode(data).decode("ascii")
        assets.append(entry)

    return {
        "version": PACKAGE_VERSION,
        "export_type": PACKAGE_EXPORT_TYPE,
        "exported_at": datetime.now(UTC).isoformat(),
        "source_platform": platform_name,
        "presentation": {
            "source_id": str(presentation.id),
            "title": presentation.title,
            "kind": _spec_kind(spec),
            "aspect_ratio": spec.get("aspect_ratio"),
            "theme": spec.get("theme"),
            "slide_count": int(presentation.slide_count or 0),
            "revision": int(presentation.revision or 1),
            "template": (
                {
                    "slug": _template_slug(container),
                    "name": container.name,
                    "kind": container.kind,
                }
                if container is not None
                else None
            ),
            "spec": spec,
            "assets": assets,
        },
    }


def _spec_kind(spec: dict[str, Any]) -> str:
    from app.services.presentation_spec import presentation_kind

    return presentation_kind(spec.get("aspect_ratio"))


def package_filename(presentation: Presentation) -> str:
    """``<slugified title>.presentation.json`` — the download name."""
    from app.core.utils import slugify

    title = (presentation.title or "").strip()
    # ``slugify`` has its own fallback word for an empty input; a deck's
    # download name should not inherit it.
    stem = slugify(title) if title else "presentation"
    return f"{(stem or 'presentation')[:80]}.presentation.json"


# ---------------------------------------------------------------------------
# Parse
# ---------------------------------------------------------------------------


def parse_presentation_package(raw: bytes) -> PresentationPackage:
    """Decode and bound-check an uploaded bundle.

    Accepts the envelope written by :func:`build_presentation_package` and,
    for convenience, a **bare spec** (a JSON object with ``title`` and
    ``slides``) — that is what an agent's validated spec looks like, so a deck
    copied out of a trace or a chat can be imported too, without assets.
    Nothing here touches the database; every check is on shape and size.
    """
    if len(raw) > MAX_PACKAGE_BYTES:
        raise PresentationPackageError(f"The package is too large (max {MAX_PACKAGE_BYTES // (1024 * 1024)} MB).")
    try:
        document = json.loads(raw.decode("utf-8-sig"))
    except UnicodeDecodeError as exc:
        raise PresentationPackageError("The package is not UTF-8 encoded JSON.") from exc
    except json.JSONDecodeError as exc:
        raise PresentationPackageError(f"The package is not valid JSON: {exc.msg}.") from exc
    if not isinstance(document, dict):
        raise PresentationPackageError("The package must be a JSON object.")

    if "export_type" not in document and "slides" in document:
        # Bare PresentationSpec.
        return PresentationPackage(spec=document)

    export_type = document.get("export_type")
    if export_type != PACKAGE_EXPORT_TYPE:
        raise PresentationPackageError(f"This is not a presentation package (export_type={export_type!r}).")
    version = document.get("version")
    if version != PACKAGE_VERSION:
        raise PresentationPackageError(
            f"Unsupported presentation package version {version!r} (this platform reads version {PACKAGE_VERSION})."
        )
    body = document.get("presentation")
    if not isinstance(body, dict):
        raise PresentationPackageError("The package has no 'presentation' object.")
    spec = body.get("spec")
    if not isinstance(spec, dict):
        raise PresentationPackageError("The package has no 'presentation.spec' object.")

    template_slug = template_name = None
    template = body.get("template")
    if template is not None:
        if not isinstance(template, dict):
            raise PresentationPackageError("'presentation.template' must be an object or null.")
        template_slug = _optional_str(template.get("slug"), max_chars=120, what="template.slug")
        template_name = _optional_str(template.get("name"), max_chars=300, what="template.name")

    raw_assets = body.get("assets")
    if raw_assets is None:
        raw_assets = []
    if not isinstance(raw_assets, list):
        raise PresentationPackageError("'presentation.assets' must be a list.")
    if len(raw_assets) > MAX_PACKAGE_ASSETS:
        raise PresentationPackageError(
            f"The package lists too many assets ({len(raw_assets)}; max {MAX_PACKAGE_ASSETS})."
        )

    assets: list[PackagedAsset] = []
    seen_ids: set[str] = set()
    for index, entry in enumerate(raw_assets):
        if not isinstance(entry, dict):
            raise PresentationPackageError(f"assets[{index}] must be an object.")
        original_id = entry.get("id")
        if not isinstance(original_id, str) or not original_id.strip():
            raise PresentationPackageError(f"assets[{index}].id is required.")
        original_id = original_id.strip()
        if original_id in seen_ids:
            raise PresentationPackageError(f"assets[{index}].id {original_id!r} is listed twice.")
        seen_ids.add(original_id)
        declared = _optional_digest(entry.get("sha256"), what=f"assets[{index}].sha256")
        missing = entry.get("missing")
        if missing is not None and not isinstance(missing, bool):
            # The import gives a missing reference a fresh id instead of
            # resolving it, so only the boolean the exporter writes may say so.
            raise PresentationPackageError(f"assets[{index}].missing must be true or false.")
        if missing is True:
            # The exporting side already knew this reference dangled.
            assets.append(PackagedAsset(original_id=original_id, filename=safe_filename(None), missing=True))
            continue
        filename = safe_filename(entry.get("filename"), fallback=f"asset-{index + 1}")
        content_type = _optional_str(entry.get("content_type"), max_chars=100, what=f"assets[{index}].content_type")
        owning = entry.get("template")
        owning_slug = owning_name = None
        if owning is not None:
            if not isinstance(owning, dict):
                raise PresentationPackageError(f"assets[{index}].template must be an object or null.")
            owning_slug = _optional_str(owning.get("slug"), max_chars=120, what=f"assets[{index}].template.slug")
            owning_name = _optional_str(owning.get("name"), max_chars=300, what=f"assets[{index}].template.name")
        data: bytes | None = None
        encoded = entry.get("data_base64")
        if encoded is not None:
            if not isinstance(encoded, str):
                raise PresentationPackageError(f"assets[{index}].data_base64 must be a string.")
            # 4 base64 chars encode 3 bytes; refuse before decoding so a
            # multi-GB string is never materialised.
            if len(encoded) > (MAX_EMBEDDED_ASSET_BYTES * 4) // 3 + 4:
                raise PresentationPackageError(
                    f"Embedded file {filename!r} is too large (max {MAX_EMBEDDED_ASSET_BYTES // (1024 * 1024)} MB)."
                )
            try:
                data = base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise PresentationPackageError(f"Embedded file {filename!r} is not valid base64.") from exc
            if len(data) > MAX_EMBEDDED_ASSET_BYTES:
                raise PresentationPackageError(
                    f"Embedded file {filename!r} is too large (max {MAX_EMBEDDED_ASSET_BYTES // (1024 * 1024)} MB)."
                )
            if declared and _sha256_hex(data) != declared:
                raise PresentationPackageError(f"Embedded file {filename!r} does not match its declared sha256 digest.")
        assets.append(
            PackagedAsset(
                original_id=original_id,
                filename=filename,
                content_type=content_type,
                sha256=declared or (_sha256_hex(data) if data is not None else None),
                asset_role=_optional_str(entry.get("asset_role"), max_chars=50, what=f"assets[{index}].asset_role"),
                description=_optional_str(
                    entry.get("description"), max_chars=2000, what=f"assets[{index}].description"
                ),
                template_slug=owning_slug,
                template_name=owning_name,
                data=data,
            )
        )

    return PresentationPackage(
        spec=spec,
        template_slug=template_slug,
        template_name=template_name,
        assets=assets,
        source_platform=_optional_str(document.get("source_platform"), max_chars=200, what="source_platform"),
    )


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------


async def _find_template_container(
    db: AsyncSession,
    *,
    slug: str | None,
    name: str | None,
    user,
) -> TemplateContainer | None:
    """The local container matching a packaged template, by slug then by name.

    The slug lookup is the built-in one (seeded ``slug:`` tag, company-managed),
    so a brand deck finds the brand deck. The name lookup is bounded to what the
    importer can see — an import must not style a deck with a template its
    owner could not open in the Templates library. That is the three-tier
    predicate for everyone, admins included: a name match is a lookup,
    not a by-id read, and the admin bypass would bind the deck to a colleague's
    private template that the importer's own library does not list, that the
    editor's re-link (``load_visible_template_facts``) refuses, and that no
    other reader can resolve a palette from.
    """
    from app.api.v1.helpers import get_user_group_ids, user_visible_filter
    from app.services.template_container_seed import builtin_container_filter

    if slug:
        result = await db.execute(select(TemplateContainer).where(builtin_container_filter(slug)).limit(1))
        found = result.scalars().first()
        if found is not None:
            return found
    if name:
        group_ids = await get_user_group_ids(db, user.id)
        result = await db.execute(
            select(TemplateContainer)
            .where(
                user_visible_filter(TemplateContainer, user.id, group_ids),
                TemplateContainer.name == name,
            )
            .order_by(TemplateContainer.is_company_managed.desc(), TemplateContainer.created_at.asc())
            .limit(1)
        )
        found = result.scalars().first()
        if found is not None:
            return found
    return None


@dataclass(slots=True)
class _ContentReads:
    """The namesakes ONE import has read to compare them with packaged files.

    A namesake is downloaded at most once per import - a dark and a light
    ``logo.svg`` in one package compare the same rows - and the downloads
    together stay under :data:`MAX_CONTENT_MATCH_DOWNLOADS` objects and
    :data:`MAX_CONTENT_MATCH_READ_BYTES` bytes.
    """

    digests: dict[uuid.UUID, str | None] = field(default_factory=dict)
    bytes_read: int = 0
    downloads: int = 0

    async def digest_of(self, row: FileAttachment) -> str | None:
        """The SHA-256 of *row*'s stored bytes, ``None`` when they cannot be read."""
        if row.id in self.digests:
            return self.digests[row.id]
        if (
            self.downloads >= MAX_CONTENT_MATCH_DOWNLOADS
            or self.bytes_read + int(row.size or 0) > MAX_CONTENT_MATCH_READ_BYTES
        ):
            return None
        from app.services.file_storage import file_storage

        self.downloads += 1
        try:
            data = await file_storage.download(row.s3_object_name)
        except Exception as exc:  # a lost object cannot confirm anything
            logger.warning(
                "Presentation import: could not read template asset %s to compare it (%s)",
                row.id,
                format_exception_message(exc),
            )
            self.digests[row.id] = None
            return None
        self.bytes_read += len(data)
        digest = await asyncio.to_thread(_sha256_hex, data)
        self.digests[row.id] = digest
        return digest


@dataclass(slots=True)
class _AssetMatch:
    """The local asset a packaged file resolves to, and how it was singled out."""

    row: FileAttachment
    #: Assets of the template sharing the packaged filename, ``row`` included.
    namesakes: int
    #: The packaged digest picked ``row`` among several namesakes.
    by_content: bool = False

    @property
    def by_name_only(self) -> bool:
        """Several namesakes, and nothing confirmed which one the package means."""
        return self.namesakes > 1 and not self.by_content


async def _find_template_asset(
    db: AsyncSession,
    *,
    container: TemplateContainer,
    filename: str,
    sha256: str | None,
    reads: _ContentReads,
) -> _AssetMatch | None:
    """The asset of *container* a packaged file resolves to: by name, then content.

    The candidates are the template's own assets with the packaged filename, in
    the order the template lists them (oldest, then id), so the choice never
    depends on the order the database returns rows in. A lone namesake is the
    match whatever its bytes: it is this install's version of the file, which is
    how a deck lands on the destination's own brand logos. Several are told
    apart by the packaged digest (:func:`_namesake_holding`); when none is
    confirmed to hold it, the first one listed is the match.
    """
    result = await db.execute(
        select(FileAttachment)
        .where(
            FileAttachment.template_container_id == container.id,
            FileAttachment.source == TEMPLATE_ASSET_SOURCE,
            FileAttachment.filename == filename,
        )
        .order_by(FileAttachment.created_at.asc(), FileAttachment.id.asc())
    )
    namesakes = list(result.scalars().all())
    if not namesakes:
        return None
    digest = _canonical_digest(sha256)
    if len(namesakes) > 1 and digest is not None:
        holder = await _namesake_holding(namesakes, digest, reads)
        if holder is not None:
            return _AssetMatch(holder, namesakes=len(namesakes), by_content=True)
    return _AssetMatch(namesakes[0], namesakes=len(namesakes))


async def _namesake_holding(
    namesakes: list[FileAttachment], digest: str, reads: _ContentReads
) -> FileAttachment | None:
    """The namesake whose stored bytes have *digest*, or ``None``.

    What a row records comes first, at no cost: ``FileAttachment.sha256`` is
    the digest of its bytes (every write that points a row at an object sets it,
    a replace in place included - ``tests/test_file_attachment_digest.py``) and
    is compared exactly; a seeded key carries only a PREFIX of it
    (``seeded_digest_prefix``), which rules the row out when it
    does not start *digest* and only makes the row likely when it does. Every
    row not ruled out is then read - likely ones first, at most
    :data:`MAX_CONTENT_MATCH_READS` - and confirmed on the full digest, so a
    prefix collision is never taken for the packaged file. An uploaded asset's
    key carries no digest, so its bytes are the only way to know.
    """
    from app.services.template_asset_seed import seeded_digest_prefix

    likely: list[FileAttachment] = []
    unknown: list[FileAttachment] = []
    for row in namesakes:
        recorded = _canonical_digest(row.sha256)
        if recorded is not None:
            if recorded == digest:
                return row
            continue
        prefix = seeded_digest_prefix(row.s3_object_name)
        if prefix is None:
            unknown.append(row)
        elif digest.startswith(prefix):
            likely.append(row)
    for row in (likely + unknown)[:MAX_CONTENT_MATCH_READS]:
        if await reads.digest_of(row) == digest:
            return row
    return None


async def _resolve_asset(
    db: AsyncSession,
    asset: PackagedAsset,
    *,
    user,
    template_cache: dict[tuple[str | None, str | None], TemplateContainer | None],
    reads: _ContentReads,
    report: ImportReport,
) -> uuid.UUID | None:
    """Resolve one packaged asset to a local file id, matching before copying."""
    key = (asset.template_slug, asset.template_name)
    if (asset.template_slug or asset.template_name) and key not in template_cache:
        template_cache[key] = await _find_template_container(
            db, slug=asset.template_slug, name=asset.template_name, user=user
        )
    container = template_cache.get(key)
    if container is not None:
        match = await _find_template_asset(
            db, container=container, filename=asset.filename, sha256=asset.sha256, reads=reads
        )
        if match is not None:
            report.assets_matched += 1
            if match.by_name_only:
                warning = (
                    f"{match.namesakes} assets of {container.name!r} are named {asset.filename!r} and none was "
                    "confirmed to hold the packaged file, so the deck uses the one the template lists first."
                )
                if warning not in report.warnings:
                    report.warnings.append(warning)
            return match.row.id

    if asset.data is None:
        return None

    from app.services.file_storage import file_storage
    from app.services.image_geometry import image_geometry
    from app.services.svg_sanitizer import SvgSanitizeError, sanitize_template_asset

    content_type = asset.content_type or guess_content_type(asset.filename)
    try:
        clean = sanitize_template_asset(asset.data, content_type, asset.filename)
    except SvgSanitizeError as exc:
        raise PresentationPackageError(f"Embedded file {asset.filename!r} is not a valid SVG: {exc}") from exc
    object_name = await file_storage.upload(
        data=clean,
        filename=asset.filename,
        content_type=content_type,
        prefix=f"presentation-import/{user.id}",
    )
    row = FileAttachment(
        id=uuid.uuid4(),  # assigned here so the id is known before the flush
        user_id=user.id,
        filename=asset.filename,
        content_type=content_type,
        size=len(clean),
        s3_object_name=object_name,
        sha256=await asyncio.to_thread(_sha256_hex, clean),
        source=PRESENTATION_IMPORT_SOURCE,
        asset_role=asset.asset_role,
        description=asset.description,
        # The trim box a logo wall draws the mark by, read from the bytes.
        image_meta=await image_geometry(clean, content_type),
    )
    db.add(row)
    await db.flush()
    report.assets_copied += 1
    return row.id


async def import_presentation_package(
    db: AsyncSession,
    package: PresentationPackage,
    *,
    user,
    title: str | None = None,
) -> tuple[Presentation, ImportReport]:
    """Create a deck owned by *user* from a parsed package.

    Resolution, in order, for the template: packaged slug -> packaged name ->
    the install's default template -> none. For each referenced file: the asset
    of the matching local template with its filename (the one holding its bytes
    when several share it, see :func:`_find_template_asset`) -> the embedded
    bytes, uploaded as a new file owned by the importer -> refusal naming the
    file.
    The spec is then remapped and saved through the same validated path an
    agent uses, so nothing an import can carry bypasses ``validate_spec``.

    Raises :class:`PresentationPackageError` for an unresolvable reference and
    :class:`~app.services.presentation_spec.PresentationSpecError` for a spec
    the validator rejects — both before any deck row exists. Files uploaded
    for an import that then fails are left to the caller's transaction (the
    ``FileAttachment`` rows roll back with it).
    """
    from app.services.presentation_legacy import keep_stored_logo_labels
    from app.services.presentation_spec import MAX_SLIDE_CAP
    from app.services.presentations import resolve_default_template_id, save_presentation
    from app.services.template_package import remap_asset_ids

    report = ImportReport(assets_total=len(package.assets))
    # A package is a STORED spec: one written before `show_labels`
    # keeps the labels its logo grids drew.
    spec = keep_stored_logo_labels(json.loads(json.dumps(package.spec)))  # private copy, JSON-safe

    if title:
        spec["title"] = title.strip()[:300] or spec.get("title")

    template_cache: dict[tuple[str | None, str | None], TemplateContainer | None] = {}
    container: TemplateContainer | None = None
    if package.template_slug or package.template_name:
        container = await _find_template_container(
            db, slug=package.template_slug, name=package.template_name, user=user
        )
        template_cache[(package.template_slug, package.template_name)] = container
    if container is not None:
        report.template = "matched"
        report.template_name = container.name
    else:
        default_id = await resolve_default_template_id(db)
        if default_id is not None:
            container = await db.get(TemplateContainer, default_id)
        if container is not None:
            report.template = "default"
            report.template_name = container.name
            if package.template_slug or package.template_name:
                report.warnings.append(
                    f"Template {package.template_name or package.template_slug!r} is not available here; "
                    f"the deck uses {container.name!r} instead."
                )
        elif package.template_slug or package.template_name:
            report.warnings.append(
                f"Template {package.template_name or package.template_slug!r} is not available here; "
                "the deck has no template."
            )

    referenced = set(_walk_file_reference_ids(spec))
    id_map: dict[str, str] = {}
    missing: list[str] = []
    gone_ids: set[str] = set()
    packaged_ids = {asset.original_id for asset in package.assets}
    reads = _ContentReads()
    for asset in package.assets:
        if asset.original_id not in referenced:
            # Carried along but no longer referenced — nothing to resolve.
            report.assets_total -= 1
            continue
        if asset.missing:
            # The picture was already gone on the exporting side (a removed
            # template asset): it goes with the import instead of
            # refusing it. A fresh id names no file anywhere, so the import
            # never probes whether the exported id exists on this install.
            gone_id = str(uuid.uuid4())
            id_map[asset.original_id] = gone_id
            gone_ids.add(gone_id)
            continue
        resolved = await _resolve_asset(db, asset, user=user, template_cache=template_cache, reads=reads, report=report)
        if resolved is None:
            missing.append(asset.filename)
            continue
        id_map[asset.original_id] = str(resolved)
    unlisted = sorted(referenced - packaged_ids)
    if unlisted:
        missing.extend(f"file {file_id}" for file_id in unlisted)
    if missing:
        shown = ", ".join(missing[:5]) + (" ..." if len(missing) > 5 else "")
        raise PresentationPackageError(
            f"{len(missing)} referenced file(s) could not be found or copied: {shown}. "
            "Re-export the deck with its assets embedded, or attach the missing files to a "
            "template available on this platform."
        )

    if id_map:
        spec = remap_asset_ids(spec, id_map)
    if gone_ids:
        count = len(gone_ids)
        report.warnings.append(
            f"{count} picture{'s were' if count != 1 else ' was'} already missing from the exported deck; "
            "nothing is drawn in their place."
        )

    summary = "Imported from a package"
    if package.source_platform:
        summary = f"Imported from {package.source_platform}"
    presentation = await save_presentation(
        db,
        user_id=user.id,
        spec_payload=spec,
        template_container_id=container.id if container is not None else None,
        verify_files_for=user,
        verify_carried=gone_ids,
        author_id=user.id,
        change_source=VERSION_SOURCE_IMPORT,
        change_summary=summary,
        # The deck was accepted where it was exported: held to the
        # ceiling only, like a duplicate; it cannot grow past its template's
        # cap here until it is back under it.
        slide_cap=MAX_SLIDE_CAP,
    )
    return presentation, report


__all__ = [
    "MAX_EMBEDDED_ASSET_BYTES",
    "MAX_PACKAGE_ASSETS",
    "MAX_PACKAGE_BYTES",
    "PACKAGE_EXPORT_TYPE",
    "PACKAGE_VERSION",
    "PRESENTATION_IMPORT_SOURCE",
    "ImportReport",
    "PackagedAsset",
    "PresentationPackage",
    "PresentationPackageError",
    "build_presentation_package",
    "import_presentation_package",
    "package_filename",
    "parse_presentation_package",
    "safe_filename",
]
