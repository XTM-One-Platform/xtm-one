# SPDX-License-Identifier: Apache-2.0

"""Software as an entity of the canonical asset store (#1495): sync and read.

The asset store (``organization_asset_store``) rebuilds the installations of an
organization (``organization_asset_software``: a vendor, product and version on
an asset) from its sources on every profile save, SBOM import or endpoint read.
This module keeps, across those rebuilds, what they install:

- :func:`sync_software` runs at the end of every rebuild: one software per
  vendor and product (``organization_software``, its id stable), one version per
  software and version (``organization_software_versions``, ``unknown`` when no
  source knows it), each installation linked to its version, and the
  dependencies the SBOMs give (``organization_software_dependencies``:
  ``depends_on`` and ``bundled_in``). A synced software nothing installs any
  more is removed; a software an administrator typed (``origin`` ``manual``)
  stays with its versions.
- :func:`software_rows` is the pure half of the sync (testable without a database).
- :func:`list_software`, :func:`get_software` and :func:`assets_of_version` are
  the paginated reads of the API (``GET /api/v1/organization-context/software``).

The CPE of a software is resolved, never guessed (``identity.resolve_cpe``): the
administrator's override, else one a source reported, else ``unresolved``.
Design: ``dev-docs/asset-model.md`` sections 6 and 9.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.organization_asset import (
    OrganizationAsset,
    OrganizationAssetSoftware,
    OrganizationSoftware,
    OrganizationSoftwareDependency,
    OrganizationSoftwareVersion,
)
from app.services import organization_asset_identity as identity

#: What a software is (STIX 2.1 Software SCO plus the XTM Platform's SaaS).
SOFTWARE_TYPES: tuple[str, ...] = (
    "application",
    "operating_system",
    "library",
    "firmware",
    "container_image",
    "saas",
)
#: ``synced``: a source reports it (read-only to people); ``manual``: typed by an administrator.
ORIGINS: tuple[str, ...] = ("synced", "manual")
DEPENDS_ON = "depends_on"
BUNDLED_IN = "bundled_in"
DEPENDENCY_KINDS: tuple[str, ...] = (DEPENDS_ON, BUNDLED_IN)
#: The most rows a page answers; a larger ``limit`` is clamped.
MAX_PAGE = 200
#: The most versions one walk of the dependency graph visits (a cycle or a huge graph stops there).
MAX_WALK = 5_000

#: A software key: case-folded vendor and product.
Key = tuple[str, str]
#: An installed version: case-folded vendor, product and the version as stored.
VersionKey = tuple[str, str, str]


def _key(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


def _cut(value: Any, size: int) -> str:
    return str(value or "").strip()[:size]


def _limit(limit: int) -> int:
    return max(1, min(int(limit or 50), MAX_PAGE))


def product_cpe(cpe: str) -> str:
    """The product-level form of a CPE 2.3 (version ``*``); a value that is not CPE 2.3 is answered as is."""
    parts = cpe.split(":")
    if len(parts) >= 6 and parts[0] == "cpe" and parts[1] == "2.3":
        parts[5] = "*"
        return ":".join(parts)
    return cpe


def version_cpe(cpe: str, version: str) -> str:
    """*cpe* with *version* set, when the CPE is 2.3 and the version known; else empty."""
    parts = cpe.split(":")
    if len(parts) >= 6 and parts[0] == "cpe" and parts[1] == "2.3" and version and version != identity.UNKNOWN_VERSION:
        parts[5] = version
        return ":".join(parts)
    return ""


def product_purl(purl: str) -> str:
    """A Package URL without its ``@version`` (the software's, not one version's)."""
    head, _, _ = purl.partition("?")
    base, at, _ = head.rpartition("@")
    return base if at and "/" in base else head


def display_name(vendor: str, product: str) -> str:
    """``vendor product`` unless the product already starts with the vendor (``Microsoft Teams``)."""
    vendor, product = vendor.strip(), product.strip()
    if not vendor or _key(product).startswith(_key(vendor)):
        return product
    return f"{vendor} {product}"


@dataclass
class SoftwareRow:
    """What the sync writes for one software (pure half of :func:`sync_software`)."""

    vendor: str
    product: str
    sources: set[str] = field(default_factory=set)
    reported_cpes: set[str] = field(default_factory=set)
    purl: str = ""
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    #: version -> {cpe, purl, first_seen, last_seen}
    versions: dict[str, dict[str, Any]] = field(default_factory=dict)


def _earliest(a: datetime | None, b: datetime | None) -> datetime | None:
    return b if a is None else a if b is None else min(a, b)


def _latest(a: datetime | None, b: datetime | None) -> datetime | None:
    return b if a is None else a if b is None else max(a, b)


def software_rows(installations: Iterable[Mapping[str, Any]]) -> dict[Key, SoftwareRow]:
    """The software and versions *installations* install (pure, testable).

    Each installation holds ``vendor``, ``product``, ``version``, ``cpe``,
    ``purl``, ``sources``, ``first_seen`` and ``last_seen``. The cased spelling
    of a vendor or product wins; a CPE a source reported is kept per version and
    as a candidate for the software's product-level CPE.
    """
    out: dict[Key, SoftwareRow] = {}
    for inst in installations:
        product = str(inst.get("product") or "").strip()
        if not product:
            continue
        vendor = str(inst.get("vendor") or "").strip()
        key = (_key(vendor), _key(product))
        row = out.get(key)
        if row is None:
            row = out[key] = SoftwareRow(vendor=vendor, product=product)
        row.vendor = identity._spelling(row.vendor, vendor)
        row.product = identity._spelling(row.product, product)
        row.sources.update(str(s) for s in inst.get("sources") or [] if s)
        cpe = str(inst.get("cpe") or "").strip()
        purl = str(inst.get("purl") or "").strip()
        if cpe:
            row.reported_cpes.add(product_cpe(cpe))
        if purl and not row.purl:
            row.purl = product_purl(purl)
        first, last = inst.get("first_seen"), inst.get("last_seen")
        row.first_seen = _earliest(row.first_seen, first)
        row.last_seen = _latest(row.last_seen, last)
        version = str(inst.get("version") or "").strip() or identity.UNKNOWN_VERSION
        entry = row.versions.setdefault(version, {"cpe": "", "purl": "", "first_seen": None, "last_seen": None})
        if cpe and not entry["cpe"]:
            entry["cpe"] = cpe
        if purl and not entry["purl"]:
            entry["purl"] = purl
        entry["first_seen"] = _earliest(entry["first_seen"], first)
        entry["last_seen"] = _latest(entry["last_seen"], last)
    return out


def dependency_edges(software: Sequence[Mapping[str, Any]]) -> list[tuple[VersionKey, VersionKey, str]]:
    """The edges the software of ONE asset gives: ``(version, target version, kind)`` (pure, testable).

    ``bundled_in`` and ``depends_on`` name their targets by ``product version``
    (or ``product`` alone when the version is unknown), the labels the SBOM
    parser writes; a label resolves only to software of the same asset, and a
    label that names nothing there is dropped.
    """
    labels: dict[str, VersionKey] = {}
    for soft in software:
        product = str(soft.get("product") or "").strip()
        if not product:
            continue
        version = str(soft.get("version") or "").strip() or identity.UNKNOWN_VERSION
        vkey = (_key(soft.get("vendor")), _key(product), version)
        labels.setdefault(_key(f"{product} {version}"), vkey)
        if version == identity.UNKNOWN_VERSION:
            labels.setdefault(_key(product), vkey)
    edges: list[tuple[VersionKey, VersionKey, str]] = []
    for soft in software:
        product = str(soft.get("product") or "").strip()
        if not product:
            continue
        version = str(soft.get("version") or "").strip() or identity.UNKNOWN_VERSION
        source = (_key(soft.get("vendor")), _key(product), version)
        targets = [(label, DEPENDS_ON) for label in soft.get("depends_on") or []]
        if soft.get("bundled_in"):
            targets.append((soft["bundled_in"], BUNDLED_IN))
        for label, kind in targets:
            target = labels.get(_key(label))
            if target is not None and target != source and (source, target, kind) not in edges:
                edges.append((source, target, kind))
    return edges


async def sync_software(
    db: AsyncSession,
    organization_id: uuid.UUID,
    edges: Iterable[tuple[VersionKey, VersionKey, str]] = (),
) -> dict[str, int]:
    """Bring the software, versions and dependencies of *organization_id* in line with its installations.

    Runs after a rebuild wrote the installations (the caller commits). Answers
    counts: ``software``, ``versions``, ``dependencies`` written and ``removed``.
    """
    installs = (
        (
            await db.execute(
                select(OrganizationAssetSoftware).where(OrganizationAssetSoftware.organization_id == organization_id)
            )
        )
        .scalars()
        .all()
    )
    rows = software_rows(
        {
            "vendor": i.vendor,
            "product": i.product,
            "version": i.version,
            "cpe": i.cpe,
            "purl": i.purl,
            "sources": i.sources,
            "first_seen": i.first_seen_at,
            "last_seen": i.last_seen_at,
        }
        for i in installs
    )
    stored = {
        (s.vendor_key, s.product_key): s
        for s in (
            await db.execute(
                select(OrganizationSoftware).where(OrganizationSoftware.organization_id == organization_id)
            )
        )
        .scalars()
        .all()
    }
    removed = 0
    for key, soft in list(stored.items()):
        if key not in rows and soft.origin != "manual":
            await db.delete(soft)
            del stored[key]
            removed += 1
    for key, row in rows.items():
        existing = stored.get(key)
        if existing is not None:
            soft = existing
        else:
            soft = OrganizationSoftware(
                id=uuid.uuid4(),
                organization_id=organization_id,
                vendor_key=key[0][:200],
                product_key=key[1][:200],
                origin="synced",
                software_type="application",
            )
            db.add(soft)
            stored[key] = soft
        soft.vendor = _cut(row.vendor, 200)
        soft.product = _cut(row.product, 200)
        soft.name = _cut(display_name(row.vendor, row.product), 300)
        soft.name_key = _key(soft.name)[:300]
        soft.sources = sorted(row.sources | ({"manual"} if soft.origin == "manual" else set()))
        soft.purl = _cut(soft.purl or row.purl, 500) if soft.origin == "manual" else _cut(row.purl, 500)
        soft.first_seen_at = row.first_seen
        soft.last_seen_at = row.last_seen
        resolved = identity.resolve_cpe(
            row.vendor, row.product, "", reported=row.reported_cpes, override=soft.cpe_override or ""
        )
        soft.cpe = _cut(resolved["cpe"], 300)
        soft.cpe_state = resolved["confidence"]
    await db.flush()
    versions = {
        (v.software_id, v.version): v
        for v in (
            await db.execute(
                select(OrganizationSoftwareVersion).where(
                    OrganizationSoftwareVersion.organization_id == organization_id
                )
            )
        )
        .scalars()
        .all()
    }
    wanted: set[tuple[uuid.UUID, str]] = set()
    by_version_key: dict[VersionKey, uuid.UUID] = {}
    written = 0
    for key, row in rows.items():
        soft = stored[key]
        for version, entry in row.versions.items():
            vkey = (soft.id, _cut(version, 100))
            wanted.add(vkey)
            ver = versions.get(vkey)
            if ver is None:
                ver = OrganizationSoftwareVersion(
                    id=uuid.uuid4(), organization_id=organization_id, software_id=soft.id, version=vkey[1]
                )
                db.add(ver)
                versions[vkey] = ver
                written += 1
            ver.cpe = _cut(entry["cpe"] or version_cpe(soft.cpe, version), 300)
            ver.purl = _cut(entry["purl"], 500)
            ver.first_seen_at = entry["first_seen"]
            ver.last_seen_at = entry["last_seen"]
            by_version_key[(key[0], key[1], version)] = ver.id
    manual = {s.id for s in stored.values() if s.origin == "manual"}
    for vkey, ver in list(versions.items()):
        if vkey not in wanted and ver.software_id not in manual:
            await db.delete(ver)
            removed += 1
    await db.flush()
    for inst in installs:
        version = inst.version or identity.UNKNOWN_VERSION
        inst.software_version_id = by_version_key.get((inst.vendor_key, inst.product_key, version))
    # The edges are rebuilt whole with the installations: an SBOM removed takes its edges with it.
    await db.execute(
        delete(OrganizationSoftwareDependency).where(OrganizationSoftwareDependency.organization_id == organization_id)
    )
    seen: set[tuple[uuid.UUID, uuid.UUID, str]] = set()
    dependencies = 0
    for source, target, kind in edges:
        src = by_version_key.get((source[0], source[1], source[2]))
        dst = by_version_key.get((target[0], target[1], target[2]))
        if src is None or dst is None or src == dst or (src, dst, kind) in seen:
            continue
        seen.add((src, dst, kind))
        db.add(
            OrganizationSoftwareDependency(
                organization_id=organization_id,
                version_id=src,
                depends_on_version_id=dst,
                kind=kind,
                sources=["sbom"],
            )
        )
        dependencies += 1
    await db.flush()
    return {"software": len(rows), "versions": written, "dependencies": dependencies, "removed": removed}


# ── Reads ─────────────────────────────────────────────────────────────


@dataclass
class Page:
    items: list[dict[str, Any]]
    total: int
    offset: int
    limit: int


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _software_dict(
    soft: OrganizationSoftware, *, versions: int = 0, assets: int = 0, unknown: bool = False
) -> dict[str, Any]:
    return {
        "id": str(soft.id),
        "name": soft.name or display_name(soft.vendor, soft.product),
        "vendor": soft.vendor,
        "product": soft.product,
        "type": soft.software_type,
        "description": soft.description or "",
        "cpe": soft.cpe,
        "cpe_state": soft.cpe_state,
        "cpe_override": soft.cpe_override or "",
        "purl": soft.purl,
        "origin": soft.origin,
        "editable": soft.origin == "manual",
        "sources": list(soft.sources or []),
        "version_count": versions,
        "asset_count": assets,
        "has_unknown_version": unknown,
        "first_seen": _iso(soft.first_seen_at),
        "last_seen": _iso(soft.last_seen_at),
    }


async def list_software(
    db: AsyncSession,
    organization_id: uuid.UUID,
    *,
    q: str = "",
    software_type: str = "",
    vendor: str = "",
    source: str = "",
    origin: str = "",
    cpe_state: str = "",
    unknown_version: bool | None = None,
    offset: int = 0,
    limit: int = 50,
) -> Page:
    """The software of *organization_id*, by name, with its version and asset counts, filtered and paginated."""
    limit, offset = _limit(limit), max(0, int(offset or 0))
    soft = OrganizationSoftware
    ver = OrganizationSoftwareVersion
    inst = OrganizationAssetSoftware
    where: list[Any] = [soft.organization_id == organization_id]
    if q.strip():
        needle = f"%{_key(q)}%"
        where.append(or_(soft.name_key.like(needle), soft.vendor_key.like(needle), soft.product_key.like(needle)))
    if software_type.strip():
        where.append(soft.software_type == software_type.strip())
    if vendor.strip():
        where.append(soft.vendor_key == _key(vendor))
    if origin.strip():
        where.append(soft.origin == origin.strip())
    if cpe_state.strip():
        where.append(soft.cpe_state == cpe_state.strip())
    if source.strip():
        where.append(soft.sources.contains([source.strip()]))
    unknown_ids = select(ver.software_id).where(
        ver.organization_id == organization_id, ver.version == identity.UNKNOWN_VERSION
    )
    if unknown_version is True:
        where.append(soft.id.in_(unknown_ids))
    elif unknown_version is False:
        where.append(soft.id.not_in(unknown_ids))
    total = (await db.execute(select(func.count()).select_from(soft).where(*where))).scalar() or 0
    page = (
        (await db.execute(select(soft).where(*where).order_by(soft.name_key, soft.id).offset(offset).limit(limit)))
        .scalars()
        .all()
    )
    ids = [s.id for s in page]
    counts: dict[uuid.UUID, tuple[int, bool]] = {}
    assets: dict[uuid.UUID, int] = {}
    if ids:
        for sid, n, unknown in (
            await db.execute(
                select(
                    ver.software_id,
                    func.count(ver.id),
                    func.bool_or(ver.version == identity.UNKNOWN_VERSION),
                )
                .where(ver.software_id.in_(ids))
                .group_by(ver.software_id)
            )
        ).all():
            counts[sid] = (int(n), bool(unknown))
        for sid, n in (
            await db.execute(
                select(ver.software_id, func.count(func.distinct(inst.asset_id)))
                .join(inst, inst.software_version_id == ver.id)
                .where(ver.software_id.in_(ids))
                .group_by(ver.software_id)
            )
        ).all():
            assets[sid] = int(n)
    items = [
        _software_dict(
            s,
            versions=counts.get(s.id, (0, False))[0],
            assets=assets.get(s.id, 0),
            unknown=counts.get(s.id, (0, False))[1],
        )
        for s in page
    ]
    return Page(items=items, total=int(total), offset=offset, limit=limit)


async def _edges(db: AsyncSession, version_ids: Sequence[uuid.UUID], *, reverse: bool) -> list[dict[str, Any]]:
    """The dependencies of *version_ids* (``reverse``: the versions that depend on them), with the software named."""
    if not version_ids:
        return []
    dep = OrganizationSoftwareDependency
    ver = OrganizationSoftwareVersion
    soft = OrganizationSoftware
    mine, other = (
        (dep.depends_on_version_id, dep.version_id) if reverse else (dep.version_id, dep.depends_on_version_id)
    )
    rows = (
        await db.execute(
            select(dep.kind, dep.scope, mine, ver.id, ver.version, soft.id, soft.name)
            .join(ver, ver.id == other)
            .join(soft, soft.id == ver.software_id)
            .where(mine.in_(version_ids))
            .order_by(soft.name_key, ver.version)
        )
    ).all()
    return [
        {
            "kind": kind,
            "scope": scope,
            "from_version_id": str(own),
            "version_id": str(vid),
            "version": version,
            "software_id": str(sid),
            "software_name": name,
        }
        for kind, scope, own, vid, version, sid, name in rows
    ]


async def get_software(
    db: AsyncSession,
    organization_id: uuid.UUID,
    software_id: uuid.UUID,
    *,
    installations_offset: int = 0,
    installations_limit: int = 100,
) -> dict[str, Any] | None:
    """One software: its versions with asset counts, a page of its installations, what it depends on and what uses it."""
    soft = (
        await db.execute(
            select(OrganizationSoftware).where(
                OrganizationSoftware.organization_id == organization_id, OrganizationSoftware.id == software_id
            )
        )
    ).scalar_one_or_none()
    if soft is None:
        return None
    ver = OrganizationSoftwareVersion
    inst = OrganizationAssetSoftware
    versions = (await db.execute(select(ver).where(ver.software_id == soft.id).order_by(ver.version))).scalars().all()
    ids = [v.id for v in versions]
    per_version = (
        {
            vid: int(n)
            for vid, n in (
                await db.execute(
                    select(inst.software_version_id, func.count(func.distinct(inst.asset_id)))
                    .where(inst.software_version_id.in_(ids))
                    .group_by(inst.software_version_id)
                )
            ).all()
        }
        if ids
        else {}
    )
    limit, offset = _limit(installations_limit), max(0, int(installations_offset or 0))
    where = inst.software_version_id.in_(ids) if ids else inst.id.is_(None)
    total = (await db.execute(select(func.count()).select_from(inst).where(where))).scalar() or 0
    rows = (
        await db.execute(
            select(inst, OrganizationAsset.name)
            .join(OrganizationAsset, OrganizationAsset.id == inst.asset_id)
            .where(where)
            .order_by(OrganizationAsset.name_key, inst.version, OrganizationAsset.id)
            .offset(offset)
            .limit(limit)
        )
    ).all()
    asset_count = (await db.execute(select(func.count(func.distinct(inst.asset_id))).where(where))).scalar() or 0
    return {
        **_software_dict(
            soft,
            versions=len(versions),
            assets=int(asset_count),
            unknown=any(v.version == identity.UNKNOWN_VERSION for v in versions),
        ),
        "versions": [
            {
                "id": str(v.id),
                "version": v.version,
                "cpe": v.cpe,
                "purl": v.purl,
                "eol": v.eol or None,
                "asset_count": per_version.get(v.id, 0),
                "first_seen": _iso(v.first_seen_at),
                "last_seen": _iso(v.last_seen_at),
            }
            for v in versions
        ],
        "installations": {
            "items": [
                {
                    "asset_id": str(i.asset_id),
                    "asset_name": name,
                    "version_id": str(i.software_version_id) if i.software_version_id else None,
                    "version": i.version,
                    "install_date": i.install_date or None,
                    "bundled_in": i.bundled_in or None,
                    "sources": list(i.sources or []),
                    "first_seen": _iso(i.first_seen_at),
                    "last_seen": _iso(i.last_seen_at),
                }
                for i, name in rows
            ],
            "total": int(total),
            "offset": offset,
            "limit": limit,
        },
        "depends_on": await _edges(db, ids, reverse=False),
        "used_by": await _edges(db, ids, reverse=True),
    }


async def assets_of_version(
    db: AsyncSession,
    organization_id: uuid.UUID,
    version_id: uuid.UUID,
    *,
    through_dependencies: bool = True,
    offset: int = 0,
    limit: int = 50,
) -> Page | None:
    """The assets where a software version is present, directly or (``through_dependencies``) under one that needs it.

    Each item names the asset, the version installed there and ``via``: empty
    when the version itself is installed, else the installed version that
    depends on it (directly or transitively). ``None``: no such version.
    """
    ver = OrganizationSoftwareVersion
    found = (
        await db.execute(select(ver.id).where(ver.organization_id == organization_id, ver.id == version_id))
    ).scalar_one_or_none()
    if found is None:
        return None
    dep = OrganizationSoftwareDependency
    reach: dict[uuid.UUID, uuid.UUID | None] = {version_id: None}
    frontier = [version_id]
    while through_dependencies and frontier and len(reach) < MAX_WALK:
        edges = (
            await db.execute(
                select(dep.version_id, dep.depends_on_version_id).where(dep.depends_on_version_id.in_(frontier))
            )
        ).all()
        frontier = []
        for user, _target in edges:
            if user not in reach:
                reach[user] = user
                frontier.append(user)
    inst = OrganizationAssetSoftware
    limit, offset = _limit(limit), max(0, int(offset or 0))
    where = [inst.organization_id == organization_id, inst.software_version_id.in_(list(reach))]
    total = (await db.execute(select(func.count()).select_from(inst).where(*where))).scalar() or 0
    rows = (
        await db.execute(
            select(inst, OrganizationAsset.name)
            .join(OrganizationAsset, OrganizationAsset.id == inst.asset_id)
            .where(*where)
            .order_by(OrganizationAsset.name_key, inst.product_key, inst.version, OrganizationAsset.id)
            .offset(offset)
            .limit(limit)
        )
    ).all()
    return Page(
        items=[
            {
                "asset_id": str(i.asset_id),
                "asset_name": name,
                "product": i.product,
                "version": i.version,
                "via": None if i.software_version_id == version_id else f"{i.product} {i.version}",
            }
            for i, name in rows
        ],
        total=int(total),
        offset=offset,
        limit=limit,
    )
