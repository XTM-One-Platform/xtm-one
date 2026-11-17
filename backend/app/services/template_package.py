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

"""The template container package format — export / import.

A ``TemplateContainer`` is metadata *plus binaries*: the palette and the
layout catalog live in JSON columns, but the fonts, logos and background images
are ``FileAttachment`` rows in S3. So a template that travels as JSON alone is
not a template — the logo is the brand. The complete form is a ZIP:

    template-container.json     metadata + the asset manifest
    assets/<filename>           one entry per binary

This layout is deliberately the one already sitting in
``dev-docs/presentation-template-package/`` so the dev fixture becomes a real
importable package rather than a parallel format that can drift from it.

Why the manifest carries ``original_id``
----------------------------------------
Imported assets get **new** ``FileAttachment`` rows, so every stored reference to
an old asset id is dangling the moment it lands. ``layouts`` and
``extra_metadata`` are consumer-defined JSON that may hold ``image_file_id`` /
``background_image_file_id`` values (a deck built from the template references
them by id), so the import remaps old ids to new ones — see :func:`remap_asset_ids`.
Without that pass a template imports "successfully" and renders with missing
images, which is the worst available failure mode because nothing errors.

Parsing an uploaded archive is parsing attacker-controlled input, so
:func:`parse_package` is bounded on every axis (entry count, uncompressed size,
per-asset size, path shape) rather than trusting the archive's own headers.
"""

from __future__ import annotations

import io
import json
import posixpath
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.core.archive_reading import ArchiveTooLargeError, read_bounded

#: Bundle envelope, matching every other entity's export contract
#: (``{version, export_type, exported_at, <entity>: {…}}``).
PACKAGE_VERSION = 1
PACKAGE_EXPORT_TYPE = "template_container"

#: The metadata document inside the archive.
MANIFEST_NAME = "template-container.json"
#: Every binary lives under this prefix; anything else in the archive is ignored.
ASSET_DIR = "assets/"

#: Fields that describe the *template*. Tenancy (``user_id``,
#: ``is_company_managed``, ``shared_group_ids``) and instance statistics
#: (``lookup_count``) are deliberately absent: they describe this deployment, not
#: the design, and carrying them would let an import claim a visibility the
#: importer is not entitled to.
EXPORTED_FIELDS: tuple[str, ...] = (
    "name",
    "description",
    "kind",
    "tags",
    "theme",
    "theme_config",
    "aspect_ratio",
    "layouts",
    "extra_metadata",
)

#: Bounds on an uploaded archive. A zip bomb is the obvious risk of unpacking
#: someone else's file, and the entry cap also keeps a 10 000-file archive from
#: spending minutes in the loop before the asset cap rejects it.
MAX_PACKAGE_BYTES = 64 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 128 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 512
#: The manifest is JSON metadata. 2 MB is already absurd for it, and bounding it
#: separately is what keeps a highly-compressible manifest from being the hole in
#: the uncompressed budget (it is read before any asset, so it cannot be covered
#: by a running total that starts after it).
MAX_MANIFEST_BYTES = 2 * 1024 * 1024


def _read_bounded(archive: zipfile.ZipFile, entry: zipfile.ZipInfo, limit: int) -> bytes:
    """Read at most *limit* bytes from *entry*, or raise if it holds more (``read_bounded``)."""
    try:
        return read_bounded(archive, entry, limit)
    except ArchiveTooLargeError as exc:
        raise TemplatePackageError(f"Package entry {exc} and was refused.") from exc


class TemplatePackageError(ValueError):
    """An archive or bundle that cannot be imported, with a readable reason."""


@dataclass(slots=True)
class PackagedAsset:
    """One binary in a package, with the metadata needed to re-create its row."""

    filename: str
    data: bytes
    asset_role: str | None = None
    #: What the asset depicts — travels with the binary so an imported
    #: or duplicated template keeps the captions its agents choose assets by.
    description: str | None = None
    content_type: str | None = None
    #: The asset's id in the *exporting* instance. Used only to remap stored
    #: references; never reused as the new row's id.
    original_id: str | None = None


@dataclass(slots=True)
class TemplatePackage:
    """A parsed package: the container metadata plus its assets."""

    container: dict[str, Any]
    assets: list[PackagedAsset] = field(default_factory=list)


def _safe_asset_name(path: str) -> str | None:
    """Return the bare filename for an ``assets/`` entry, or None to skip it.

    Rejects anything that is not a direct child of ``assets/``: absolute paths,
    ``..`` traversal, nested directories and Windows separators. The import never
    writes to a filesystem — assets go to object storage under a generated key —
    but a traversing name would still end up as the stored ``filename``, which is
    later echoed into a ``Content-Disposition`` header on download.
    """
    normalized = path.replace("\\", "/")
    if not normalized.startswith(ASSET_DIR):
        return None
    relative = normalized[len(ASSET_DIR) :]
    if not relative or relative.endswith("/"):
        return None
    # ``normpath`` collapses ``a/../b``; anything that still contains a separator
    # or climbs out is refused rather than flattened, so the archive cannot smuggle
    # a name the exporter would never produce.
    cleaned = posixpath.normpath(relative)
    if cleaned != relative or "/" in cleaned or cleaned.startswith("."):
        return None
    return cleaned


def build_manifest(
    container: dict[str, Any],
    assets: list[dict[str, Any]],
    *,
    exported_at: datetime | None = None,
) -> dict[str, Any]:
    """Build the JSON bundle for *container*, with an asset manifest."""
    payload = {key: container.get(key) for key in EXPORTED_FIELDS}
    payload["assets"] = assets
    return {
        "version": PACKAGE_VERSION,
        "export_type": PACKAGE_EXPORT_TYPE,
        "exported_at": (exported_at or datetime.now(UTC)).isoformat(),
        PACKAGE_EXPORT_TYPE: payload,
    }


def build_package_zip(
    container: dict[str, Any],
    assets: list[PackagedAsset],
    *,
    exported_at: datetime | None = None,
) -> bytes:
    """Serialize a container and its assets to a package archive."""
    manifest_assets: list[dict[str, Any]] = []
    used: set[str] = set()
    for asset in assets:
        # Two assets may legitimately share a filename (the same logo.svg added
        # twice under different roles). The archive cannot, so later duplicates
        # are suffixed and the manifest records the name actually written.
        #
        # The suffix is always applied to the ORIGINAL base, never to the last
        # candidate: suffixing the candidate gives `asset-2-3` for the third
        # duplicate of an extension-less name, growing with every collision.
        original = asset.filename or "asset"
        stem, dot, ext = original.rpartition(".")
        base = stem if dot else original
        suffix = f"{dot}{ext}" if dot else ""
        name = original
        counter = 2
        while name in used:
            name = f"{base}-{counter}{suffix}"
            counter += 1
        used.add(name)
        manifest_assets.append(
            {
                "path": f"{ASSET_DIR}{name}",
                "filename": name,
                "asset_role": asset.asset_role,
                "description": asset.description,
                "content_type": asset.content_type,
                "size": len(asset.data),
                "original_id": asset.original_id,
            }
        )

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            MANIFEST_NAME,
            json.dumps(
                build_manifest(container, manifest_assets, exported_at=exported_at),
                ensure_ascii=False,
                indent=2,
            ),
        )
        for asset, entry in zip(assets, manifest_assets, strict=True):
            archive.writestr(entry["path"], asset.data)
    return buffer.getvalue()


#: The JSON type each exported field must have if present. The persistence layer
#: calls ``.strip()`` on the text ones and iterates the collections, so a bundle
#: carrying ``{"description": {...}}`` would raise deep inside the import and
#: surface as a 500. Refusing it here makes it a 422 with a readable reason —
#: which is the whole difference between "your file is wrong" and "we crashed".
_FIELD_TYPES: tuple[tuple[str, type | tuple[type, ...], str], ...] = (
    ("name", str, "a string"),
    ("description", str, "a string"),
    ("kind", str, "a string"),
    ("theme", str, "a string"),
    ("aspect_ratio", str, "a string"),
    ("tags", list, "an array of strings"),
    ("layouts", list, "an array of layout objects"),
    ("theme_config", dict, "an object of theme keys"),
    ("extra_metadata", dict, "an object"),
)


def _check_field_types(container: dict[str, Any]) -> None:
    """Refuse a bundle whose fields are present but of the wrong JSON type."""
    problems = [
        f"'{field}' must be {described}, got {type(container[field]).__name__}"
        for field, expected, described in _FIELD_TYPES
        if container.get(field) is not None and not isinstance(container[field], expected)
    ]
    if problems:
        raise TemplatePackageError(
            "The export bundle has fields of the wrong type: " + "; ".join(problems) + "."
        )


def parse_bundle(bundle: object) -> TemplatePackage:
    """Validate a metadata-only JSON bundle (``format=json`` export)."""
    if not isinstance(bundle, dict):
        raise TemplatePackageError(
            "The export bundle must be a JSON object with 'version', "
            f"'export_type' and '{PACKAGE_EXPORT_TYPE}' keys."
        )
    version = bundle.get("version")
    if version != PACKAGE_VERSION:
        raise TemplatePackageError(f"Unsupported export version: {version!r}")
    export_type = bundle.get("export_type")
    if export_type != PACKAGE_EXPORT_TYPE:
        raise TemplatePackageError(f"Unsupported export type: {export_type!r}")
    container = bundle.get(PACKAGE_EXPORT_TYPE)
    if not isinstance(container, dict):
        raise TemplatePackageError(
            f"'{PACKAGE_EXPORT_TYPE}' must be an object holding the template "
            "fields."
        )
    _check_field_types(container)
    if not str(container.get("name") or "").strip():
        raise TemplatePackageError("Template name is required.")
    return TemplatePackage(container=container, assets=[])


@contextmanager
def _unreadable_as_package_error() -> Iterator[None]:
    """A member that cannot be read as it declares (a damaged stream, encryption, an
    unknown compression) is a ``BadZipFile`` from ``read_bounded``: reported
    as a package error, the route's 422, never a 500 nor a package too large."""
    try:
        yield
    except zipfile.BadZipFile as exc:
        raise TemplatePackageError(f"The package is not a readable ZIP archive ({exc}).") from exc


def parse_package(raw: bytes) -> TemplatePackage:
    """Parse an uploaded package: either a ZIP archive or a raw JSON bundle.

    Accepts both so one import endpoint serves both export formats. Every bound
    is checked against what was actually read, never against the archive's own
    declared sizes — those are attacker-controlled.
    """
    if not raw:
        raise TemplatePackageError("The uploaded file is empty.")
    if len(raw) > MAX_PACKAGE_BYTES:
        raise TemplatePackageError(
            f"Package too large ({len(raw):,} bytes). Maximum is "
            f"{MAX_PACKAGE_BYTES // (1024 * 1024)} MB."
        )

    if not zipfile.is_zipfile(io.BytesIO(raw)):
        # A metadata-only export is plain JSON, so fall back to it rather than
        # telling the user their valid ``format=json`` file is "not a ZIP".
        try:
            return parse_bundle(json.loads(raw.decode("utf-8")))
        except TemplatePackageError:
            raise
        except (ValueError, UnicodeDecodeError) as exc:
            raise TemplatePackageError(
                "The file is neither a template package ZIP nor a JSON export "
                "bundle."
            ) from exc

    with _unreadable_as_package_error(), zipfile.ZipFile(io.BytesIO(raw)) as archive:
        entries = archive.infolist()
        if len(entries) > MAX_ARCHIVE_ENTRIES:
            raise TemplatePackageError(
                f"Package holds too many entries ({len(entries)}). Maximum is "
                f"{MAX_ARCHIVE_ENTRIES}."
            )

        manifest_entry = next(
            (e for e in entries if posixpath.basename(e.filename) == MANIFEST_NAME),
            None,
        )
        if manifest_entry is None:
            raise TemplatePackageError(
                f"Package is missing its '{MANIFEST_NAME}' manifest."
            )
        # A manifest may sit at the archive root or one directory down, because
        # zipping a folder is what a human actually does.
        prefix = manifest_entry.filename[: -len(MANIFEST_NAME)]

        try:
            manifest_raw = _read_bounded(archive, manifest_entry, MAX_MANIFEST_BYTES)
            package = parse_bundle(json.loads(manifest_raw))
        except (ValueError, UnicodeDecodeError) as exc:
            if isinstance(exc, TemplatePackageError):
                raise
            raise TemplatePackageError(
                f"'{MANIFEST_NAME}' is not valid JSON."
            ) from exc

        declared = {
            str(item.get("filename")): item
            for item in package.container.get("assets") or []
            if isinstance(item, dict) and item.get("filename")
        }

        # The manifest counts against the same budget as the assets: it is part of
        # what the archive expands to.
        total = len(manifest_raw)
        for entry in entries:
            if entry is manifest_entry or entry.is_dir():
                continue
            relative = (
                entry.filename[len(prefix) :]
                if prefix and entry.filename.startswith(prefix)
                else entry.filename
            )
            name = _safe_asset_name(relative)
            if name is None:
                # Not an asset (a README, an OS metadata file, or a rejected
                # path). Ignored rather than fatal: refusing a package because it
                # carries a .DS_Store would be useless strictness.
                continue
            remaining = MAX_UNCOMPRESSED_BYTES - total
            if remaining <= 0:
                raise TemplatePackageError(
                    "Package expands to more than "
                    f"{MAX_UNCOMPRESSED_BYTES // (1024 * 1024)} MB and was "
                    "refused."
                )
            try:
                data = _read_bounded(archive, entry, remaining)
            except TemplatePackageError as exc:
                raise TemplatePackageError(
                    "Package expands to more than "
                    f"{MAX_UNCOMPRESSED_BYTES // (1024 * 1024)} MB and was "
                    f"refused ({exc})."
                ) from exc
            total += len(data)
            meta = declared.get(name, {})
            package.assets.append(
                PackagedAsset(
                    filename=name,
                    data=data,
                    asset_role=meta.get("asset_role"),
                    # Hostile input: bounded and coerced on persist
                    # (``normalize_asset_description``), so a non-string or a
                    # multi-kilobyte value here cannot land as is.
                    description=(
                        meta["description"] if isinstance(meta.get("description"), str) else None
                    ),
                    content_type=meta.get("content_type"),
                    original_id=(
                        str(meta["original_id"]) if meta.get("original_id") else None
                    ),
                )
            )

    # The manifest's ``assets`` list describes what the archive should hold; the
    # rows are built from the bytes actually found, so the key is dropped to keep
    # a stale manifest from being mistaken for container metadata.
    package.container.pop("assets", None)
    return package


def remap_asset_ids(value: Any, mapping: dict[str, str]) -> Any:
    """Rewrite every occurrence of an old asset id to its new one.

    Walks ``layouts`` / ``extra_metadata`` — consumer-defined JSON of unknown
    shape — and substitutes whole string values only. Substring rewriting is
    deliberately avoided: an id embedded in prose is not a reference, and
    rewriting it would corrupt the text.
    """
    if not mapping:
        return value
    if isinstance(value, str):
        return mapping.get(value, value)
    if isinstance(value, list):
        return [remap_asset_ids(item, mapping) for item in value]
    if isinstance(value, dict):
        return {key: remap_asset_ids(item, mapping) for key, item in value.items()}
    return value
