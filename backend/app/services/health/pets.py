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

"""The Concierge companion's pets (#8261): the Codex pet package format, read as hostile input.

``pet.json`` and a spritesheet of :data:`COLUMNS` columns of
:data:`CELL_WIDTH` x :data:`CELL_HEIGHT` cells, one state a row
(:data:`STATES`), uploaded as one ``.zip`` or as the two files. Every check an
upload passes, and the code each refusal answers, is the table of
``dev-docs/concierge-companion.md``. The platform stores the spritesheet it
decoded and re-encoded itself, never the uploaded bytes. The built-in pets
(:data:`BUILTIN_SLUGS`) ship as files, drawn by
``backend/scripts/generate_alfred_pet.py``; they have no row.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import posixpath
import unicodedata
import zipfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.health_pet import ConciergePet

logger = logging.getLogger(__name__)

COLUMNS: Final = 8
CELL_WIDTH: Final = 192
CELL_HEIGHT: Final = 208
ROWS_BY_VERSION: Final[dict[int, int]] = {1: 9, 2: 11}
ATLAS_SIZE: Final[dict[int, tuple[int, int]]] = {
    version: (COLUMNS * CELL_WIDTH, rows * CELL_HEIGHT) for version, rows in ROWS_BY_VERSION.items()
}
#: ``(state, row, frame durations in ms)``, rows 0-8 of every version.
STATES: Final[tuple[tuple[str, int, tuple[int, ...]], ...]] = (
    ("idle", 0, (280, 110, 110, 140, 140, 320)),
    ("running-right", 1, (120, 120, 120, 120, 120, 120, 120, 220)),
    ("running-left", 2, (120, 120, 120, 120, 120, 120, 120, 220)),
    ("waving", 3, (140, 140, 140, 280)),
    ("jumping", 4, (140, 140, 140, 140, 280)),
    ("failed", 5, (140, 140, 140, 140, 140, 140, 140, 240)),
    ("waiting", 6, (150, 150, 150, 150, 150, 260)),
    ("running", 7, (120, 120, 120, 120, 120, 220)),
    ("review", 8, (150, 150, 150, 150, 150, 280)),
)

MAX_PACKAGE_BYTES: Final = 8 * 1024 * 1024
MAX_MANIFEST_BYTES: Final = 16 * 1024
MAX_ZIP_ENTRIES: Final = 32

BUILTIN_DIR: Final = Path(__file__).resolve().parents[2] / "agents" / "seed_data" / "concierge_pets"
#: Shipped with the platform, listed first; Alfred is every user's default.
BUILTIN_SLUGS: Final[tuple[str, ...]] = ("alfred",)
DEFAULT_PET: Final = "alfred"

PetErrorCode = Literal[
    "too_large",
    "not_a_package",
    "manifest",
    "spritesheet_missing",
    "image_type",
    "image_size",
    "image_unreadable",
    "slug_taken",
]
ERROR_MESSAGES: Final[dict[str, str]] = {
    "too_large": "The package is larger than 8 MB.",
    "not_a_package": "Upload one .zip holding pet.json and its spritesheet, or pet.json and the spritesheet.",
    "manifest": "pet.json is missing or does not follow the pet format.",
    "spritesheet_missing": "The spritesheet pet.json names is not in the package.",
    "image_type": "The spritesheet must be a single-frame WebP or PNG image.",
    "image_size": "The spritesheet must be 1536x1872 (version 1) or 1536x2288 (version 2).",
    "image_unreadable": "The spritesheet could not be read.",
    "slug_taken": "A pet with this id is already in the library.",
}

_PNG_MAGIC: Final = b"\x89PNG\r\n\x1a\n"
#: Control, format (bidi overrides, zero-width), surrogate, private and unassigned characters, line separators.
_REFUSED_CATEGORIES: Final = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})


class PetPackageError(ValueError):
    def __init__(self, code: PetErrorCode, detail: str = "") -> None:
        self.code = code
        self.message = ERROR_MESSAGES[code]
        super().__init__(f"{code}: {detail}" if detail else code)


def _plain(value: str) -> str:
    value = value.strip()
    if any(unicodedata.category(char) in _REFUSED_CATEGORIES for char in value):
        raise ValueError("control characters")
    return value


class PetManifest(BaseModel):
    """``pet.json``: the fields the format defines, bounded; any other key is ignored."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True, strict=True)

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    display_name: str = Field(alias="displayName", min_length=1, max_length=64)
    description: str | None = Field(default=None, max_length=280)
    spritesheet_path: str = Field(alias="spritesheetPath", min_length=1, max_length=128)
    sprite_version: Literal[1, 2] = Field(default=1, alias="spriteVersionNumber")

    @field_validator("display_name")
    @classmethod
    def _display_name(cls, value: str) -> str:
        value = _plain(value)
        if not value:
            raise ValueError("empty")
        return value

    @field_validator("description")
    @classmethod
    def _description(cls, value: str | None) -> str | None:
        return (_plain(value) or None) if value is not None else None

    @field_validator("spritesheet_path")
    @classmethod
    def _path(cls, value: str) -> str:
        value = _plain(value)
        normal = posixpath.normpath(value)
        if (
            "\\" in value
            or value.startswith("/")
            or normal.startswith("..")
            or normal != value.removeprefix("./")
            or not normal.lower().endswith((".webp", ".png"))
        ):
            raise ValueError("not a relative .webp or .png path inside the package")
        return normal


@dataclass(frozen=True)
class ParsedPet:
    manifest: PetManifest
    #: The platform's own WebP encoding of the spritesheet.
    spritesheet: bytes
    sha256: str


def _manifest(data: bytes) -> PetManifest:
    if len(data) > MAX_MANIFEST_BYTES:
        raise PetPackageError("manifest", "pet.json too large")
    try:
        raw = json.loads(data.decode("utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("not an object")
        return PetManifest.model_validate(raw)
    except (UnicodeDecodeError, ValueError, ValidationError) as exc:
        raise PetPackageError("manifest", type(exc).__name__) from exc


def _image_format(data: bytes) -> str | None:
    if data.startswith(_PNG_MAGIC):
        return "PNG"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "WEBP"
    return None


def reencode_spritesheet(data: bytes, version: int) -> bytes:
    """*data* decoded and re-encoded as lossless WebP, once it is a single-frame atlas of *version*'s size."""
    from PIL import Image, UnidentifiedImageError

    magic = _image_format(data)
    if magic is None:
        raise PetPackageError("image_type", "magic bytes")
    try:
        with Image.open(io.BytesIO(data), formats=[magic]) as image:
            if image.format != magic or getattr(image, "n_frames", 1) != 1:
                raise PetPackageError("image_type", f"{image.format} frames={getattr(image, 'n_frames', 1)}")
            # Read from the header: no pixel is decoded before the size is right.
            if image.size != ATLAS_SIZE[version]:
                raise PetPackageError("image_size", f"{image.size} for version {version}")
            image.load()
            atlas = image.convert("RGBA")
    except PetPackageError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        raise PetPackageError("image_unreadable", type(exc).__name__) from exc
    out = io.BytesIO()
    atlas.save(out, format="WEBP", lossless=True, quality=100, method=4)
    return out.getvalue()


def _read_zip(data: bytes) -> tuple[str, bytes, dict[str, zipfile.ZipInfo], zipfile.ZipFile]:
    """``(pet.json's entry name, its bytes, the entries by name, the archive)``."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
        entries = [info for info in archive.infolist() if not info.is_dir()]
    except (zipfile.BadZipFile, OSError, ValueError) as exc:
        raise PetPackageError("not_a_package", type(exc).__name__) from exc
    if len(entries) > MAX_ZIP_ENTRIES:
        raise PetPackageError("not_a_package", f"{len(entries)} entries")
    by_name = {info.filename: info for info in entries}
    manifests = [name for name in by_name if posixpath.basename(name) == "pet.json" and name.count("/") <= 1]
    if len(manifests) != 1:
        raise PetPackageError("manifest", f"{len(manifests)} pet.json")
    return manifests[0], _bounded(archive, by_name[manifests[0]], MAX_MANIFEST_BYTES, "manifest"), by_name, archive


def _bounded(archive: zipfile.ZipFile, info: zipfile.ZipInfo, limit: int, code: PetErrorCode) -> bytes:
    """An entry's bytes, at most *limit*, whatever its header declares."""
    try:
        with archive.open(info) as handle:
            data = handle.read(limit + 1)
    except (zipfile.BadZipFile, OSError, RuntimeError, ValueError, NotImplementedError) as exc:
        raise PetPackageError("not_a_package", type(exc).__name__) from exc
    if len(data) > limit:
        raise PetPackageError(code, "entry too large")
    return data


def parse_package(files: list[tuple[str, bytes]]) -> ParsedPet:
    """The pet *files* make: one ``.zip``, or ``pet.json`` and its spritesheet. Raises :class:`PetPackageError`."""
    if any(len(data) > MAX_PACKAGE_BYTES for _, data in files):
        raise PetPackageError("too_large")
    if len(files) == 1 and files[0][1].startswith(b"PK\x03\x04"):
        manifest_name, manifest_bytes, entries, archive = _read_zip(files[0][1])
        manifest = _manifest(manifest_bytes)
        sheet_name = posixpath.normpath(posixpath.join(posixpath.dirname(manifest_name), manifest.spritesheet_path))
        info = entries.get(sheet_name)
        if info is None:
            raise PetPackageError("spritesheet_missing")
        sheet = _bounded(archive, info, MAX_PACKAGE_BYTES, "too_large")
    elif len(files) == 2:
        manifests = [data for name, data in files if posixpath.basename(name or "") == "pet.json"]
        if len(manifests) != 1:
            raise PetPackageError("not_a_package", "two files, no single pet.json")
        manifest = _manifest(manifests[0])
        sheet = next(data for name, data in files if posixpath.basename(name or "") != "pet.json")
    else:
        raise PetPackageError("not_a_package", f"{len(files)} file(s)")
    encoded = reencode_spritesheet(sheet, manifest.sprite_version)
    return ParsedPet(manifest=manifest, spritesheet=encoded, sha256=hashlib.sha256(encoded).hexdigest())


# ── The library ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PetInfo:
    slug: str
    display_name: str
    description: str | None
    sprite_version: int
    builtin: bool
    sha256: str


@lru_cache(maxsize=len(BUILTIN_SLUGS))
def _builtin(slug: str) -> tuple[PetManifest, bytes]:
    folder = BUILTIN_DIR / slug
    manifest = _manifest((folder / "pet.json").read_bytes())
    return manifest, (folder / manifest.spritesheet_path).read_bytes()


def _builtin_info(slug: str) -> PetInfo:
    manifest, sheet = _builtin(slug)
    return PetInfo(
        slug=manifest.id,
        display_name=manifest.display_name,
        description=manifest.description,
        sprite_version=manifest.sprite_version,
        builtin=True,
        sha256=hashlib.sha256(sheet).hexdigest(),
    )


async def list_pets(db: AsyncSession) -> list[PetInfo]:
    """The built-in pets, then the uploaded ones by name."""
    rows = await db.execute(
        select(
            ConciergePet.slug,
            ConciergePet.display_name,
            ConciergePet.description,
            ConciergePet.sprite_version,
            ConciergePet.spritesheet_sha256,
        ).order_by(ConciergePet.display_name, ConciergePet.slug)
    )
    uploaded = [
        PetInfo(slug=slug, display_name=name, description=text, sprite_version=version, builtin=False, sha256=sha)
        for slug, name, text, version, sha in rows.all()
    ]
    return [_builtin_info(slug) for slug in BUILTIN_SLUGS] + uploaded


async def pet_exists(db: AsyncSession, slug: str) -> bool:
    if slug in BUILTIN_SLUGS:
        return True
    return (await db.execute(select(ConciergePet.id).where(ConciergePet.slug == slug))).scalar_one_or_none() is not None


async def load_spritesheet(db: AsyncSession, slug: str) -> tuple[bytes, str] | None:
    """``(WebP bytes, sha256)`` of the pet *slug*, ``None`` when there is none."""
    if slug in BUILTIN_SLUGS:
        sheet = _builtin(slug)[1]
        return sheet, hashlib.sha256(sheet).hexdigest()
    row = (
        await db.execute(
            select(ConciergePet.spritesheet, ConciergePet.spritesheet_sha256).where(ConciergePet.slug == slug)
        )
    ).first()
    return (row[0], row[1]) if row is not None else None


async def add_pet(db: AsyncSession, parsed: ParsedPet, *, actor_id) -> ConciergePet:
    """Store *parsed* in the library. Flushed, not committed; ``slug_taken`` when its id is in use."""
    if await pet_exists(db, parsed.manifest.id):
        raise PetPackageError("slug_taken", parsed.manifest.id)
    pet = ConciergePet(
        slug=parsed.manifest.id,
        display_name=parsed.manifest.display_name,
        description=parsed.manifest.description,
        sprite_version=parsed.manifest.sprite_version,
        spritesheet=parsed.spritesheet,
        spritesheet_sha256=parsed.sha256,
        created_by_user_id=actor_id,
    )
    try:
        async with db.begin_nested():
            db.add(pet)
            await db.flush()
    except IntegrityError as exc:
        # Another upload took the id in between.
        raise PetPackageError("slug_taken", parsed.manifest.id) from exc
    return pet


async def remove_pet(db: AsyncSession, slug: str) -> ConciergePet | None:
    """Delete the uploaded pet *slug*; ``None`` when there is none (a built-in is never one). Flushed."""
    pet = (await db.execute(select(ConciergePet).where(ConciergePet.slug == slug))).scalar_one_or_none()
    if pet is not None:
        await db.delete(pet)
        await db.flush()
    return pet
