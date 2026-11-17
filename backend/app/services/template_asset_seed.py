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

"""Seed the brand ASSETS of the built-in template containers.

``template_container_seed`` syncs the metadata of the built-in brand decks;
this module uploads the binaries they need — the Filigran wordmark the deck
chrome draws on every slide, its dark-on-light variant, the product logos an
agent can place on a slide, and the PREDEFINED ASSETS a Filigran deck is made
of (architecture / product-flow diagrams, product screenshots, brand
illustrations, certification badges, team photos, the partner / integration /
customer logos) — from the manifests under
``app/agents/seed_data/brand_assets/<pack>/manifest.json``. A pack names the
container it feeds (``"container": "<slug>"``); SEVERAL packs may feed one
container, so the manifests are merged per container before anything is
compared. A pack that names no container is a LIBRARY: it feeds the
containers whose packs include it, and nothing else. The badge and the
partner / integration / customer logo libraries the Copilot templates include
are not in this repository (no release permission, #11458): they come from
the brand asset source, below.

More pack folders can sit outside the image: ``BRAND_ASSETS_EXTRA_DIRS``
names further roots, read after the bundled one, so a deployment adds packs
the public repository does not ship; the brand asset source
(``services/brand_asset_source.py``) adds one more, downloaded from a GitHub
folder, after them (Filigran's Copilot reads the logo libraries it has no
permission to publish that way). A pack folder name is one
namespace across the roots: a name a root already loaded is reported and the
later pack skipped. An entry borrowing one file (``"pack"`` on an entry) reads
it from its own root; an ``include`` reaches a pack of any root.

A container may also take a SIBLING pack's assets without copying its files: a
manifest's ``"include": [{"pack": "<folder>", "except_roles": [...]}]`` adds
the named pack's own entries after the manifest's own, minus the roles listed
(a brand that reuses the Filigran figures and logo libraries leaves the
Filigran chrome logos out, since its own ``logo`` is the one the chrome draws),
and an entry carrying ``"pack": "<folder>"`` reads its one file from that
sibling pack under the role and caption THIS manifest gives it. An include may
name the roles it takes instead (``"only_roles": [...]``): a template
that ships on every platform takes the product families of the Filigran pack
by name, so a family added to that pack later stays on the platforms it was
made for until a manifest names it. A role either list names must be an asset
role, or the include is refused: a misspelt exclusion would let its family
through. An include whose ``except_roles`` cancel its ``only_roles`` out
admits nothing and is refused too. Only a pack's own entries are included -
never what it includes in turn - so an include cannot chain or loop. A file
the manifest lists itself is not taken again from an include: its own entry is
how a template keeps its logo, or re-captions a file, over a pack it includes
whole.

Each manifest entry carries a ``description``: that caption is what an agent
picks the asset by (``get_template`` lists it next to the file id), so it must
say what the figure SHOWS and which surface (dark / light) it is drawn for, not
just name the file. Rasters ship as WebP (diagrams, screenshots), JPEG (photos)
or PNG (logos that need a transparent background and no SVG source exists);
vectors ship as SVG and are sanitized on the way in.

Idempotent by construction, without reading a hash column: every seeded asset is
stored under a DETERMINISTIC object key derived from the container id, the content
hash and the filename (:func:`seed_object_name`). A row the seed writes or
refreshes also records the full digest (``FileAttachment.sha256``) for the readers
that compare one (the presentation package import); the sync never reads
it. On each boot the expected keys are
computed from the files on disk and compared with the ``FileAttachment`` rows
already bound to the container — a match means "already there" and costs no S3
round-trip at all; a miss means the asset is new or its content changed. A new
asset is uploaded and a row inserted. A CHANGED one - same filename, new bytes -
is the same asset, refreshed in place: its row keeps its id and takes the
new object, because decks place a template asset by row id and a new row would
leave a hole on every slide showing the old one, the promise ``get_template``
makes ("asset ids are stable across a binary replacement") and the admin replace
route keeps. A seeded row whose file is no longer shipped at all is removed -
but only from a container every feeding pack of which loaded WHOLE
(:func:`incomplete_containers`): a pack an include names that did not load (a
root not mounted, a malformed manifest) or a pack with an entry that did not
load would otherwise read as "no longer shipped", and every slide placing one
of its files would lose it. Such a container keeps its rows, still takes its
new and changed files, and retires nothing until the pack loads again. An
object a row no longer names (the refreshed asset's previous one, the removed
asset's) is deleted only once the rows are committed, so a rolled-back sync
never leaves a row naming a deleted object. The object store is therefore only
touched when something actually changed. The comparison reads the SEED
TAIL of a key (``seed/<hash16>-<filename>``), not the container segment: a row
re-homed from a retired container (``template_container_seed.RETIRED_ASSET_HOMES``)
still carries the old container's segment, and must be recognised as the same
asset - same id, which is what decks reference - rather than re-uploaded and
then deleted as stale.

Failure isolation: the metadata sync has already committed when this runs, and
an object store that is slow, absent or misconfigured must not take boot down
with it. Per-container work is wrapped so one failure logs a warning and the
next boot retries; the decks then render without a logo until then, which is a
cosmetic degradation rather than an outage.

SVGs are sanitized (``svg_sanitizer``) before the hash is taken, so the stored
bytes — and the key — are those of the CLEAN document.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.file_attachment import FileAttachment
from app.models.template_container import (
    ASSET_ROLES,
    TEMPLATE_ASSET_SOURCE,
    TemplateContainer,
)
from app.services.file_storage import guess_content_type
from app.services.svg_sanitizer import SvgSanitizeError, sanitize_template_asset
from app.services.template_container_seed import (
    builtin_container_filter,
    builtin_containers_for,
)
from app.services.template_containers import normalize_asset_description

logger = logging.getLogger(__name__)

#: Where the brand manifests live: ``<pack>/manifest.json`` next to its files.
BRAND_ASSETS_DIR = Path(__file__).resolve().parents[1] / "agents" / "seed_data" / "brand_assets"

#: Object-key segment that marks a seeded asset (vs. one an admin uploaded).
SEED_KEY_SEGMENT = "seed"

#: Hex characters of the content digest a seeded key carries: a PREFIX of the
#: SHA-256, never the digest itself (see :func:`seeded_digest_prefix`).
SEED_DIGEST_PREFIX_CHARS = 16

#: Hard ceiling per seeded file. The manifests hold logos, diagrams and
#: deck-sized rasters (a 1920px WebP diagram is well under 500 KB; a photo is
#: re-encoded as JPEG before it lands here); a file above this is a packaging
#: mistake — an un-optimised export — and is skipped with a warning rather than
#: shipped into every deployment's bucket. ``tests/test_template_asset_seed.py``
#: fails the build if a shipped manifest entry would be skipped for any reason.
MAX_SEED_ASSET_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class SeedAsset:
    """One manifest entry, resolved against the files on disk."""

    filename: str
    role: str
    content_type: str
    data: bytes
    description: str = ""

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


#: What a pack folder (an ``include`` target, an entry's ``pack``) may be called.
PACK_NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")


@dataclass(frozen=True)
class PackInclude:
    """A sibling pack whose own entries a manifest's container receives too."""

    pack: str
    except_roles: frozenset[str] = frozenset()
    #: The roles taken, when the include names them; ``None`` takes every role.
    only_roles: frozenset[str] | None = None

    def admits(self, role: str) -> bool:
        return (self.only_roles is None or role in self.only_roles) and role not in self.except_roles


@dataclass(frozen=True)
class BrandManifest:
    #: The container the pack feeds; empty for a library, which only feeds through includes.
    container_slug: str
    assets: tuple[SeedAsset, ...]
    #: The pack folder the manifest lives in: what another pack's ``include`` names.
    pack: str = ""
    includes: tuple[PackInclude, ...] = ()
    #: Entries that did not load (missing, oversized or invalid file, bad entry).
    skipped: int = 0


class LoadedManifests(list[BrandManifest]):
    """The packs :func:`load_brand_manifests` read, and what it could not read.

    *unreadable* names each manifest that did not parse or is malformed, and
    each root that is not a folder. Which template such a pack feeds is
    unknown (its ``container`` was never read), so the seed retires nothing
    while there is one: its files are missing, not removed.
    """

    unreadable: tuple[str, ...] = ()


def seed_object_name(container_id: uuid.UUID, digest: str, filename: str) -> str:
    """The deterministic object key of a seeded asset.

    ``template-asset/<container>/seed/<hash16>-<filename>`` — the container
    segment matches what the REST upload path uses, so bucket listings group a
    container's assets together whoever uploaded them; the ``seed`` segment is
    what tells a seeded row from an admin's.
    """
    return f"template-asset/{container_id}/{SEED_KEY_SEGMENT}/{seed_tail(digest, filename)}"


def seed_tail(digest: str, filename: str) -> str:
    """The container-independent half of a seeded key: ``<hash16>-<filename>``.

    What :func:`_sync_container_assets` compares, so a row re-homed from a
    retired container (whose key still carries that container's segment) is
    recognised as the asset it is.
    """
    return f"{digest[:SEED_DIGEST_PREFIX_CHARS]}-{filename}"


_SEED_MARKER = f"/{SEED_KEY_SEGMENT}/"
_DIGEST_PREFIX_RE = re.compile(rf"[0-9a-f]{{{SEED_DIGEST_PREFIX_CHARS}}}")


def seeded_key_tail(key: str | None) -> str | None:
    """The seed tail of a stored object key, or ``None`` for an admin upload."""
    if not isinstance(key, str) or not key.startswith("template-asset/"):
        return None
    _, marker, tail = key.partition(_SEED_MARKER)
    if not marker or not tail:
        return None
    return tail


def seeded_digest_prefix(key: str | None) -> str | None:
    """The head of the SHA-256 a seeded object key carries, or ``None``.

    ``<hash16>`` in ``template-asset/<container>/seed/<hash16>-<filename>``:
    the first :data:`SEED_DIGEST_PREFIX_CHARS` hex characters of the digest of
    the bytes stored under that key, which the seed derives the key from. A
    reader holding a full digest compares it with ``startswith``, never as a
    substring: a mismatch proves the bytes differ, a match only makes them
    likely the same (64 bits) and is confirmed on the bytes before it is
    relied on - the presentation package import does both. ``None`` for an
    uploaded asset, whose key carries no digest at all.
    """
    tail = seeded_key_tail(key)
    if tail is None:
        return None
    prefix, dash, _filename = tail.partition("-")
    if not dash or not _DIGEST_PREFIX_RE.fullmatch(prefix):
        return None
    return prefix


def merge_manifests(manifests: list[BrandManifest]) -> dict[str, tuple[SeedAsset, ...]]:
    """Group the packs by the container they feed, in manifest order.

    A pack's ``include`` targets follow its own entries, each one the target
    pack's OWN entries in the roles it admits; an include naming no loaded
    pack adds nothing (:func:`incomplete_containers` reports it, and holds
    the container's retirements). An included file the manifest lists
    itself is left out without a report: the manifest chose its role and
    caption. A library (no container) is only ever included.

    A filename listed by two packs for the same container is kept once (the
    first wins, the duplicate is reported): two rows with the same seed tail
    could not be told apart by the sync, and one would be deleted as stale.
    """
    own_assets = {manifest.pack: manifest.assets for manifest in manifests if manifest.pack}
    merged: dict[str, list[SeedAsset]] = {}
    seen: dict[str, set[str]] = {}
    for manifest in manifests:
        if not manifest.container_slug:
            continue
        bucket = merged.setdefault(manifest.container_slug, [])
        names = seen.setdefault(manifest.container_slug, set())
        candidates = list(manifest.assets)
        listed = {asset.filename for asset in manifest.assets}
        for include in manifest.includes:
            included = own_assets.get(include.pack)
            if included is None:
                continue
            candidates.extend(
                asset for asset in included if include.admits(asset.role) and asset.filename not in listed
            )
        for asset in candidates:
            if asset.filename in names:
                logger.warning(
                    "Brand manifests for %s list %s twice; keeping the first",
                    manifest.container_slug,
                    asset.filename,
                )
                continue
            names.add(asset.filename)
            bucket.append(asset)
    return {slug: tuple(assets) for slug, assets in merged.items()}


def incomplete_containers(manifests: list[BrandManifest]) -> dict[str, tuple[str, ...]]:
    """The containers fed by a pack that did not load whole, with those packs.

    A pack feeds a container through the container's own manifests and the
    packs they include. One an include names that no root holds, or one that
    loaded with an entry skipped, makes the merged list of its container short
    of files that are still meant to ship: the sync must not read their
    absence as a retirement (:func:`seed_builtin_template_assets`). A manifest
    that could not be read at all names no container: the seed holds every
    container then (``LoadedManifests.unreadable``).
    """
    by_pack = {manifest.pack: manifest for manifest in manifests if manifest.pack}
    gaps: dict[str, list[str]] = {}
    for manifest in manifests:
        if not manifest.container_slug:
            continue
        feeding = [manifest.pack] if manifest.pack else []
        feeding.extend(include.pack for include in manifest.includes)
        bucket = gaps.setdefault(manifest.container_slug, [])
        for pack in feeding:
            fed = by_pack.get(pack)
            if (fed is None or fed.skipped) and pack not in bucket:
                bucket.append(pack)
    return {slug: tuple(packs) for slug, packs in gaps.items() if packs}


def brand_asset_roots() -> tuple[Path, ...]:
    """The bundled pack root, then the ones ``BRAND_ASSETS_EXTRA_DIRS`` names, in order."""
    from app.core.config import settings

    extra = (Path(part.strip()) for part in settings.brand_assets_extra_dirs.split(",") if part.strip())
    return (BRAND_ASSETS_DIR, *extra)


def load_brand_manifests(*roots: Path) -> LoadedManifests:
    """Parse every ``<pack>/manifest.json`` under each root, reading its files.

    No root given: :func:`brand_asset_roots`. A root that is not a folder is
    reported and skipped, and so is a pack whose folder name an earlier root
    already loaded. A manifest that is malformed, or names a file that is
    missing, oversized or (for an SVG) not well-formed, is reported and the
    offending entry skipped (counted in ``skipped``) — a packaging mistake in
    one pack must not stop the others from seeding. One :class:`BrandManifest`
    per pack; :func:`merge_manifests` folds the packs that feed one container
    together. A root that is not a folder and a manifest that cannot be read
    are named in the result's ``unreadable``.
    """
    manifests = LoadedManifests()
    unreadable: list[str] = []
    loaded: set[str] = set()
    for root in roots or brand_asset_roots():
        if not root.is_dir():
            logger.warning("Brand asset root %s is not a folder; its packs are skipped", root)
            unreadable.append(str(root))
            continue
        for manifest in _load_root(root, unreadable):
            if manifest.pack in loaded:
                logger.warning("Brand pack %s of %s is already loaded from another root; skipped", manifest.pack, root)
                continue
            loaded.add(manifest.pack)
            manifests.append(manifest)
    manifests.unreadable = tuple(unreadable)
    return manifests


def _load_root(root: Path, unreadable: list[str]) -> list[BrandManifest]:
    manifests: list[BrandManifest] = []
    for manifest_path in sorted(root.glob("*/manifest.json")):
        try:
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("Skipping unreadable brand manifest %s: %s", manifest_path, exc)
            unreadable.append(manifest_path.parent.name)
            continue
        # No ``container`` key: a library, fed to containers through includes only.
        slug = raw.get("container", "") if isinstance(raw, dict) else None
        entries = raw.get("assets") if isinstance(raw, dict) else None
        if not isinstance(slug, str) or (not slug and "container" in raw) or not isinstance(entries, list):
            logger.warning("Skipping malformed brand manifest %s", manifest_path)
            unreadable.append(manifest_path.parent.name)
            continue
        assets: list[SeedAsset] = []
        for entry in entries:
            asset = _load_entry(manifest_path.parent, entry)
            if asset is not None:
                assets.append(asset)
        manifests.append(
            BrandManifest(
                container_slug=slug,
                assets=tuple(assets),
                pack=manifest_path.parent.name,
                includes=_read_includes(manifest_path.parent.name, raw.get("include")),
                skipped=len(entries) - len(assets),
            )
        )
    return manifests


def _read_includes(pack: str, raw: object) -> tuple[PackInclude, ...]:
    """The ``include`` list of the manifest of *pack*; a malformed target is reported and skipped."""
    if raw is None:
        return ()
    if not isinstance(raw, list):
        logger.warning("Brand manifest %s: include must be a list; ignored", pack)
        return ()
    includes: list[PackInclude] = []
    for item in raw:
        target = item.get("pack") if isinstance(item, dict) else None
        except_roles = item.get("except_roles", []) if isinstance(item, dict) else None
        only_roles = item.get("only_roles") if isinstance(item, dict) else None
        if (
            not isinstance(target, str)
            or not PACK_NAME_RE.fullmatch(target)
            or target == pack
            or not _known_roles(except_roles)
            or (only_roles is not None and not (_known_roles(only_roles) and set(only_roles) - set(except_roles)))
        ):
            logger.warning("Brand manifest %s: invalid include %r; skipped", pack, item)
            continue
        includes.append(
            PackInclude(
                pack=target,
                except_roles=frozenset(except_roles),
                only_roles=frozenset(only_roles) if only_roles is not None else None,
            )
        )
    return tuple(includes)


def _known_roles(roles: object) -> bool:
    return isinstance(roles, list) and all(isinstance(role, str) and role in ASSET_ROLES for role in roles)


def _load_entry(folder: Path, entry: object) -> SeedAsset | None:
    if not isinstance(entry, dict):
        return None
    filename = entry.get("file")
    role = entry.get("role")
    if not isinstance(filename, str) or not filename or "/" in filename or "\\" in filename:
        logger.warning("Brand manifest %s: invalid asset file name %r", folder.name, filename)
        return None
    pack = entry.get("pack")
    source = folder
    if pack is not None:
        if not isinstance(pack, str) or not PACK_NAME_RE.fullmatch(pack) or pack == folder.name:
            logger.warning("Brand manifest %s: asset %s names an invalid pack %r", folder.name, filename, pack)
            return None
        source = folder.parent / pack
    if not isinstance(role, str) or role not in ASSET_ROLES:
        logger.warning(
            "Brand manifest %s: asset %s has unknown role %r (expected one of %s)",
            folder.name,
            filename,
            role,
            ", ".join(ASSET_ROLES),
        )
        return None
    path = source / filename
    try:
        data = path.read_bytes()
    except OSError as exc:
        logger.warning("Brand manifest %s: cannot read %s: %s", folder.name, filename, exc)
        return None
    if len(data) > MAX_SEED_ASSET_BYTES:
        logger.warning(
            "Brand manifest %s: %s is %d bytes, above the %d-byte seed ceiling; skipped",
            folder.name,
            filename,
            len(data),
            MAX_SEED_ASSET_BYTES,
        )
        return None
    content_type = entry.get("content_type")
    if not isinstance(content_type, str) or not content_type:
        content_type = guess_content_type(filename)
    try:
        data = sanitize_template_asset(data, content_type, filename)
    except SvgSanitizeError as exc:
        logger.warning("Brand manifest %s: %s is not valid SVG (%s); skipped", folder.name, filename, exc)
        return None
    description = entry.get("description")
    return SeedAsset(
        filename=filename,
        role=role,
        content_type=content_type,
        data=data,
        description=description if isinstance(description, str) else "",
    )


async def _load_builtin_container(db: AsyncSession, slug: str) -> TemplateContainer | None:
    result = await db.execute(select(TemplateContainer).where(builtin_container_filter(slug)))
    return result.scalars().first()


async def seed_builtin_template_assets(
    db: AsyncSession,
    *,
    platform_mode: str = "copilot",
    extra_roots: Sequence[Path] = (),
    retire: bool = True,
) -> int:
    """Upload the missing brand assets of every seeded container. Returns uploads.

    Runs AFTER :func:`template_container_seed.seed_builtin_template_containers`
    (the containers must exist) and only for the containers that ship in
    *platform_mode* — a manifest for a container the mode does not seed is
    ignored (the Filigran packs outside Copilot mode), and that
    container's assets (if any) went with it in the metadata sync's Phase 1.
    *extra_roots* are read after :func:`brand_asset_roots`: the packs the
    brand asset source downloaded (``services/brand_asset_source.py``).
    *retire* false holds every container's retirements, not only those
    :func:`incomplete_containers` names: a configured source that was not read
    may hold packs that feed a container directly, which no include reveals.
    So does a manifest that cannot be read, or a root that is not there
    (``LoadedManifests.unreadable``), for the same reason.
    """
    from app.services.file_storage import file_storage

    wanted_slugs = {c["slug"] for c in builtin_containers_for(platform_mode)}
    manifests = load_brand_manifests(*brand_asset_roots(), *extra_roots)
    unreadable = getattr(manifests, "unreadable", ())
    if unreadable and retire:
        logger.warning(
            "Brand assets: %s could not be read; every template keeps its seeded assets, none retired",
            ", ".join(unreadable),
        )
        retire = False
    incomplete = incomplete_containers(manifests)
    uploaded = 0
    for slug, assets in merge_manifests(manifests).items():
        if slug not in wanted_slugs:
            continue
        container = await _load_builtin_container(db, slug)
        if container is None:
            logger.warning("Brand assets for %s skipped: built-in container not found", slug)
            continue
        held = incomplete.get(slug, ())
        if held and retire:
            logger.warning(
                "Brand assets for %s: the pack(s) %s did not load whole; its seeded assets are kept, "
                "none retired, until they do",
                slug,
                ", ".join(held),
            )
        obsolete: list[str] = []
        try:
            uploaded += await _sync_container_assets(
                db, container, assets, file_storage, obsolete_objects=obsolete, retire=retire and not held
            )
            await db.commit()
        except Exception:  # boot must survive an object-store failure
            await db.rollback()
            logger.warning(
                "Brand assets for %s could not be synced (object store unavailable?); will retry on next startup",
                slug,
                exc_info=True,
            )
            continue
        # Only once the rows no longer name them durably do the old objects go
        # (a refreshed asset's previous object, a retired asset's object): a
        # rolled-back sync keeps every row pointing at an object that exists,
        # and a failure here leaks an object, never an asset.
        for key in obsolete:
            try:
                await file_storage.delete(key)
            except Exception:
                logger.warning("Could not delete the obsolete seeded asset object %s", key)
    return uploaded


#: How many object keys one ``IN (...)`` lookup of the sweep carries.
_SWEEP_LOOKUP_CHUNK = 500

_UUID_PATTERN = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
#: Exactly the key :func:`seed_object_name` writes, and nothing else: the sweep
#: deletes from the bucket, so a key of any other shape is never its business.
_SEEDED_KEY_RE = re.compile(
    rf"template-asset/(?P<container>{_UUID_PATTERN})/{SEED_KEY_SEGMENT}/[0-9a-f]{{{SEED_DIGEST_PREFIX_CHARS}}}-[^/]+"
)
#: One container's group in a one-level listing of ``template-asset/``.
_CONTAINER_PREFIX_RE = re.compile(rf"template-asset/(?P<container>{_UUID_PATTERN})/")


#: How often the install sweeps, whichever process runs the tick that asks.
ORPHAN_SWEEP_INTERVAL_SECS = 3600
_ORPHAN_SWEEP_LEASE_KEY = "template_asset_seed:orphan_sweep"


def _ships_every_builtin(platform_mode: str) -> bool:
    from app.services.template_container_seed import BUILTIN_TEMPLATE_CONTAINERS

    shipped = {c["slug"] for c in builtin_containers_for(platform_mode)}
    return all(c["slug"] in shipped for c in BUILTIN_TEMPLATE_CONTAINERS)


async def purge_orphaned_seed_objects_if_due(*, platform_mode: str) -> int:
    """Run :func:`purge_orphaned_seed_objects` at most once per interval, install-wide.

    Asked on every scheduler tick. The tick's hourly block counts ticks per
    process, and a process only counts the ticks whose lock it wins and starts
    again at zero after a restart, so on several replicas it is no hourly
    retry; a Redis lease is. A lease that cannot be taken (Redis unreachable)
    skips this tick, and the next one asks again. A mode that ships every
    built-in (Copilot with the Video Studio on) returns before any I/O,
    the lease included.
    """
    if _ships_every_builtin(platform_mode):
        return 0
    from app.core.redis import redis_client

    try:
        redis = await redis_client()
        if not await redis.set(_ORPHAN_SWEEP_LEASE_KEY, "1", nx=True, ex=ORPHAN_SWEEP_INTERVAL_SECS):
            return 0
    except Exception as exc:
        logger.debug("Seed orphan sweep lease not taken: %s", exc)
        return 0
    return await purge_orphaned_seed_objects(platform_mode=platform_mode)


def seeded_key_container(key: str | None) -> str | None:
    """The container id of a key of the exact seeded shape, or ``None`` for any other key."""
    match = _SEEDED_KEY_RE.fullmatch(key) if isinstance(key, str) else None
    return match["container"] if match else None


async def purge_orphaned_seed_objects(*, platform_mode: str) -> int:
    """Delete the seeded objects a removed built-in left behind. Returns objects removed.

    The durable half of removing a built-in the mode does not ship (the
    Filigran Brand Deck and its customer / partner logos and team photos
    outside Copilot mode): the removal deletes the rows in its transaction and
    purges their objects once, after the commit, stopping at the first sign of
    an unreachable store - so a store that is down at that start, a process
    killed between the commit and the purge, or a delete the store refused
    (``file_storage.delete`` swallows an S3 error answer) leaves objects no row
    names. This sweep finds them again from the bucket itself: every object of
    the exact seeded shape, ``template-asset/<container>/seed/<hash16>-<file>``,
    whose container no longer exists and that no ``FileAttachment`` row names
    is deleted - every version and filename a seed ever wrote, the re-homed
    logo libraries' keys included, with no manifest to read.

    Bounded: ``template-asset/`` is listed one level deep (one entry per
    container), and only the ``seed/`` group of a container that is gone is
    listed in full, so the hourly cost does not grow with the assets of the
    templates that exist. A store that is down or answers an error lists
    nothing and nothing is deleted; the next hour retries. The database is read
    on short sessions of its own, never held open while the store is called.
    Idempotent and safe on several replicas at once (a delete of a missing
    object succeeds). A container row is committed before the asset seed
    uploads under it and a container id is never reused, so an object the
    listing sees under a container that the next read does not find belongs to
    a container that is gone for good. A mode that ships every built-in
    (Copilot with the Video Studio on) returns before any I/O.
    """
    from app.core.database import async_session
    from app.services.file_cleanup import purge_storage_objects
    from app.services.file_storage import file_storage

    if _ships_every_builtin(platform_mode):
        return 0
    listed: set[uuid.UUID] = set()
    for prefix in await file_storage.list_object_names("template-asset/", recursive=False):
        match = _CONTAINER_PREFIX_RE.fullmatch(prefix)
        if match:
            listed.add(uuid.UUID(match["container"]))
    if not listed:
        return 0
    async with async_session() as db:
        result = await db.execute(select(TemplateContainer.id).where(TemplateContainer.id.in_(list(listed))))
        existing = set(result.scalars().all())
    candidates: list[str] = []
    for container_id in sorted(listed - existing, key=str):
        for key in await file_storage.list_object_names(f"template-asset/{container_id}/{SEED_KEY_SEGMENT}/"):
            if seeded_key_container(key) == str(container_id):
                candidates.append(key)
    if not candidates:
        return 0
    named: set[str] = set()
    async with async_session() as db:
        for start in range(0, len(candidates), _SWEEP_LOOKUP_CHUNK):
            chunk = candidates[start : start + _SWEEP_LOOKUP_CHUNK]
            result = await db.execute(
                select(FileAttachment.s3_object_name).where(FileAttachment.s3_object_name.in_(chunk))
            )
            named.update(result.scalars().all())
    orphans = [key for key in candidates if key not in named]
    if not orphans:
        return 0
    logger.info("Purging %d seeded template asset object(s) left behind by a removed built-in", len(orphans))
    return await purge_storage_objects(orphans)


async def _seed_geometry(asset: SeedAsset) -> dict:
    """The geometry of a seeded image; ``{"v": ...}`` alone for anything else.

    Never ``None``: a row the seed read is marked read, so a font or an
    unreadable file is not read again on every boot.
    """
    from app.services.image_geometry import IMAGE_META_VERSION, image_geometry

    return await image_geometry(asset.data, asset.content_type) or {"v": IMAGE_META_VERSION}


async def _sync_container_assets(
    db: AsyncSession,
    container: TemplateContainer,
    assets: Sequence[SeedAsset],
    storage,
    *,
    obsolete_objects: list[str],
    retire: bool = True,
) -> int:
    """Converge one container's seeded rows on the merged manifests. Returns uploads.

    Seeded rows are matched by the seed tail of their key
    (:func:`seeded_key_tail`), whatever container segment the key carries, so
    a row re-homed from a retired container is the same asset. Admin uploads
    (no ``seed`` segment) are never part of the comparison. A row whose file
    changed is refreshed in place; a row whose file is no longer shipped is
    deleted, unless *retire* is false (a feeding pack did not load whole, so
    a missing file does not mean a retired one). Either way the object key it no longer names is appended to
    *obsolete_objects* and nothing is deleted from the store here: the caller
    deletes them once the rows are committed, so a rolled-back sync never
    leaves a row naming an object that is gone.
    """
    result = await db.execute(
        select(FileAttachment).where(
            FileAttachment.template_container_id == container.id,
            FileAttachment.source == TEMPLATE_ASSET_SOURCE,
            FileAttachment.s3_object_name.like(f"template-asset/%{_SEED_MARKER}%"),
        )
    )
    existing_by_tail: dict[str, FileAttachment] = {}
    for row in result.scalars().all():
        tail = seeded_key_tail(row.s3_object_name)
        if tail is not None:
            existing_by_tail[tail] = row

    expected_tails = {seed_tail(asset.digest, asset.filename) for asset in assets}
    # The rows whose content is no longer shipped, by the filename their tail
    # carries (``<hash16>-<filename>``: the hash is hex, so the first dash ends
    # it). A shipped file of the same name is the same asset with new bytes.
    refreshable: dict[str, FileAttachment] = {}
    for tail, row in existing_by_tail.items():
        if tail not in expected_tails:
            refreshable.setdefault(tail.partition("-")[2], row)
    uploaded = 0
    for asset in assets:
        tail = seed_tail(asset.digest, asset.filename)
        row = existing_by_tail.get(tail)
        description = normalize_asset_description(asset.description)
        if row is not None:
            # Content unchanged. Labels are code-managed like the container's
            # metadata, so a renamed role or a reworded description in the
            # manifest still lands — the caption is what the agent picks the
            # asset by, so it must not freeze at whatever the first boot wrote.
            if row.asset_role != asset.role:
                row.asset_role = asset.role
            if row.description != description:
                row.description = description
            if row.filename != asset.filename:
                row.filename = asset.filename
            if row.user_id is None and container.user_id is not None:
                row.user_id = container.user_id
            if getattr(row, "image_meta", None) is None:
                # Seeded before the geometry existed: read it from the
                # bytes the manifest ships, once - the next boot finds it set.
                row.image_meta = await _seed_geometry(asset)
            continue
        key = seed_object_name(container.id, asset.digest, asset.filename)
        await storage.upload_to(asset.data, key, asset.content_type)
        previous = refreshable.pop(asset.filename, None)
        if previous is not None:
            # Same file, new bytes: refreshed in place, so the row id every deck
            # places keeps pointing at the asset - now at its new version.
            obsolete_objects.append(previous.s3_object_name)
            existing_by_tail.pop(seeded_key_tail(previous.s3_object_name) or "", None)
            previous.s3_object_name = key
            previous.sha256 = asset.digest
            previous.size = len(asset.data)
            previous.content_type = asset.content_type
            previous.asset_role = asset.role
            previous.description = description
            previous.filename = asset.filename
            # New bytes, new mark: the old trim box would crop the new file.
            previous.image_meta = await _seed_geometry(asset)
            if previous.user_id is None and container.user_id is not None:
                previous.user_id = container.user_id
            uploaded += 1
            logger.info("Refreshed brand asset %s (%s) in %s, same id", asset.filename, asset.role, container.name)
            continue
        db.add(
            FileAttachment(
                id=uuid.uuid4(),
                user_id=container.user_id,
                template_container_id=container.id,
                asset_role=asset.role,
                description=description,
                filename=asset.filename,
                content_type=asset.content_type,
                size=len(asset.data),
                s3_object_name=key,
                sha256=asset.digest,
                source=TEMPLATE_ASSET_SOURCE,
                image_meta=await _seed_geometry(asset),
            )
        )
        uploaded += 1
        logger.info("Seeded brand asset %s (%s) into %s", asset.filename, asset.role, container.name)

    # Retired files: a seeded row whose file the manifests no longer ship at
    # all (a changed one was refreshed above). The row goes with this sync's
    # commit; its object after it.
    if not retire:
        return uploaded
    for tail, row in existing_by_tail.items():
        if tail in expected_tails:
            continue
        obsolete_objects.append(row.s3_object_name)
        await db.delete(row)
    return uploaded
