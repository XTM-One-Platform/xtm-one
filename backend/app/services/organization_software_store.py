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
  the paginated reads of the API (``GET /api/v1/organization-context/software``);
  :func:`software_csv` its CSV export.
- :func:`create_software`, :func:`update_software`, :func:`delete_software`,
  :func:`attach_software` and :func:`detach_software` are an administrator's
  writes. A synced software is read-only but for its CPE override; a hand-typed
  one is attached to an asset through a hand document anchored on the asset's
  first source record (``organization_asset_sboms`` format ``manual``), so the
  attachment follows the asset through every rebuild and merge.

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


def software_types(software: Iterable[Mapping[str, Any]]) -> dict[Key, str]:
    """The type the sources declare for each software (pure, testable).

    An SBOM component says what it is (``library``, ``operating_system``, ...);
    a type other than ``application`` wins, a value outside :data:`SOFTWARE_TYPES`
    is ignored. A software no source types is absent (the sync keeps ``application``).
    """
    out: dict[Key, str] = {}
    for soft in software:
        product = str(soft.get("product") or "").strip()
        declared = str(soft.get("type") or "").strip()
        if not product or declared not in SOFTWARE_TYPES:
            continue
        key = (_key(soft.get("vendor")), _key(product))
        if out.get(key, "application") == "application":
            out[key] = declared
    return out


async def sync_software(
    db: AsyncSession,
    organization_id: uuid.UUID,
    edges: Iterable[tuple[VersionKey, VersionKey, str]] = (),
    types: Mapping[Key, str] | None = None,
) -> dict[str, int]:
    """Bring the software, versions and dependencies of *organization_id* in line with its installations.

    *types* (:func:`software_types`) gives a synced software the type its sources declare;
    a hand-typed software keeps the type its administrator chose.

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
        if soft.origin != "manual":
            soft.software_type = (types or {}).get(key, "application")
        soft.vendor = _cut(row.vendor, 200)
        soft.product = _cut(row.product, 200)
        # The name an administrator gave a hand-typed software stays; a synced one takes the sources'.
        if soft.origin != "manual" or not soft.name:
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


def _values(value: str | Sequence[str]) -> list[str]:
    """One filter value or several, stripped, the empty ones dropped."""
    items = [value] if isinstance(value, str) else list(value)
    return [v.strip() for v in items if v and v.strip()]


async def list_software(
    db: AsyncSession,
    organization_id: uuid.UUID,
    *,
    q: str = "",
    software_type: str | Sequence[str] = "",
    vendor: str = "",
    source: str = "",
    origin: str | Sequence[str] = "",
    cpe_state: str | Sequence[str] = "",
    unknown_version: bool | None = None,
    offset: int = 0,
    limit: int = 50,
) -> Page:
    """The software of *organization_id*, by name, with its version and asset counts, filtered and paginated.

    *software_type*, *origin* and *cpe_state* take one value or several (any of them, the Software tab's filters).
    """
    limit, offset = _limit(limit), max(0, int(offset or 0))
    soft = OrganizationSoftware
    ver = OrganizationSoftwareVersion
    inst = OrganizationAssetSoftware
    where: list[Any] = [soft.organization_id == organization_id]
    if q.strip():
        needle = f"%{_key(q)}%"
        where.append(or_(soft.name_key.like(needle), soft.vendor_key.like(needle), soft.product_key.like(needle)))
    if types := _values(software_type):
        where.append(soft.software_type.in_(types))
    if vendor.strip():
        where.append(soft.vendor_key == _key(vendor))
    if origins := _values(origin):
        where.append(soft.origin.in_(origins))
    if states := _values(cpe_state):
        where.append(soft.cpe_state.in_(states))
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
    installations_q: str = "",
) -> dict[str, Any] | None:
    """One software: its versions with asset counts, a page of its installations, what it depends on and what uses it.

    *installations_q* narrows the installations page (and its ``total``) to the assets whose name holds it;
    the software's own asset count stays the whole count.
    """
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
    page_where = [where]
    if installations_q.strip():
        page_where.append(OrganizationAsset.name_key.like(f"%{_key(installations_q)}%"))
    total = (
        await db.execute(
            select(func.count())
            .select_from(inst)
            .join(OrganizationAsset, OrganizationAsset.id == inst.asset_id)
            .where(*page_where)
        )
    ).scalar() or 0
    rows = (
        await db.execute(
            select(inst, OrganizationAsset.name)
            .join(OrganizationAsset, OrganizationAsset.id == inst.asset_id)
            .where(*page_where)
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


# ── The matcher's view: what the organization runs (#1495, slice 3) ──────


def matcher_entries(
    software: Iterable[Mapping[str, Any]], typed: Iterable[Mapping[str, Any]] = ()
) -> list[dict[str, Any]]:
    """The products the CVE matcher looks for (pure, testable): one entry per software.

    *software* holds one mapping per software entity (``vendor``, ``product``,
    ``cpe``, ``versions``: the versions installed, ``unknown`` included). The
    entry's ``version`` is the installed version when the organization runs
    exactly one known version of it, else empty (several versions, or only
    ``unknown``: the matcher then states no single installed version, never a
    guess). ``versions`` lists every known installed version (#1783), so the
    matcher checks each of them against OpenCTI's Software of that version.
    *typed* are rows typed in the organization profile before the profile moved
    to Software entities (``vendor``, ``product``, ``cpe``, ``version``): a row
    no software entity carries is still matched, so nothing typed is lost.
    Each entry carries ``product``, ``vendor``, ``cpe``, ``version``,
    ``versions``, ``label`` (the name the case and the why line show) and
    ``version_source`` (``software`` for a store entity, ``profile`` for a typed
    row: where its versions were read, #2038).
    """
    out: list[dict[str, Any]] = []
    seen: dict[Key, dict[str, Any]] = {}
    # The keys of *seen* by product: a vendor-less row finds its software in one lookup, so a
    # store of thousands of software stays linear (a scan of *seen* per row stalled the loop).
    by_product: dict[str, list[Key]] = {}

    def add(vendor: str, product: str, cpe: str, version: str, versions: list[str], source: str) -> None:
        vendor, product = vendor.strip(), product.strip()
        if not product:
            return
        key = (_key(vendor), _key(product))
        if key not in seen and not vendor:
            # A typed product with no vendor is the one listed software of that product, if only one.
            same = by_product.get(key[1]) or []
            key = same[0] if len(same) == 1 else key
        held = seen.get(key)
        if held is not None:
            # A typed row of a listed software gives its CPE when the store has none; the versions
            # stay the store's (what runs), never the typed one.
            held["cpe"] = held["cpe"] or cpe.strip()
            return
        entry = {
            "product": product,
            "vendor": vendor,
            "cpe": cpe.strip(),
            "version": version.strip(),
            "versions": versions,
            "label": display_name(vendor, product),
            "version_source": source,
        }
        seen[key] = entry
        by_product.setdefault(key[1], []).append(key)
        out.append(entry)

    for soft in software:
        known = sorted(
            {
                str(v).strip()
                for v in soft.get("versions") or ()
                if str(v or "").strip() and str(v).strip() != identity.UNKNOWN_VERSION
            }
        )
        add(
            str(soft.get("vendor") or ""),
            str(soft.get("product") or ""),
            str(soft.get("cpe") or ""),
            known[0] if len(known) == 1 else "",
            known,
            "software",
        )
    for row in typed:
        typed_version = str(row.get("version") or "").strip()
        add(
            str(row.get("vendor") or ""),
            str(row.get("product") or ""),
            str(row.get("cpe") or ""),
            typed_version,
            [typed_version] if typed_version else [],
            "profile",
        )
    return out


async def matcher_software(db: AsyncSession, organization_id: uuid.UUID) -> list[dict[str, Any]]:
    """The software entities of *organization_id* with the versions installed, for :func:`matcher_entries`.

    Two reads: the software, then its versions that an installation links
    (a hand-typed software with no installation keeps its typed versions).
    """
    soft = OrganizationSoftware
    rows = (
        await db.execute(
            select(soft.id, soft.vendor, soft.product, soft.cpe)
            .where(soft.organization_id == organization_id)
            .order_by(soft.name_key, soft.id)
        )
    ).all()
    ver = OrganizationSoftwareVersion
    versions: dict[uuid.UUID, list[str]] = {}
    for software_id, version in (
        await db.execute(select(ver.software_id, ver.version).where(ver.organization_id == organization_id))
    ).all():
        versions.setdefault(software_id, []).append(str(version))
    return [
        {"vendor": vendor, "product": product, "cpe": cpe, "versions": versions.get(sid, [])}
        for sid, vendor, product, cpe in rows
    ]


# ── Writes: an administrator's software (#1495, slice 1b) ──────────────


class SoftwareError(ValueError):
    """A software write refused; ``status`` is the HTTP status the API answers (404, 409 or 422)."""

    def __init__(self, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.status = status


def _check_cpe(cpe: str) -> str:
    cpe = (cpe or "").strip()
    if cpe and not (cpe.startswith("cpe:2.3:") and len(cpe.split(":")) >= 13):
        raise SoftwareError("cpe must be a CPE 2.3 formatted string (cpe:2.3:part:vendor:product:version:...)")
    return cpe


def _check_type(software_type: str) -> str:
    software_type = (software_type or "application").strip()
    if software_type not in SOFTWARE_TYPES:
        raise SoftwareError(f"type must be one of {', '.join(SOFTWARE_TYPES)}")
    return software_type


async def _software(db: AsyncSession, organization_id: uuid.UUID, software_id: uuid.UUID) -> OrganizationSoftware:
    soft = (
        await db.execute(
            select(OrganizationSoftware).where(
                OrganizationSoftware.organization_id == organization_id, OrganizationSoftware.id == software_id
            )
        )
    ).scalar_one_or_none()
    if soft is None:
        raise SoftwareError("Software not found", 404)
    return soft


def _resolve(soft: OrganizationSoftware) -> None:
    resolved = identity.resolve_cpe(soft.vendor, soft.product, "", reported=(), override=soft.cpe_override or "")
    if resolved["confidence"] == "override" or soft.origin == "manual":
        soft.cpe = _cut(resolved["cpe"], 300)
        soft.cpe_state = resolved["confidence"]


async def create_software(
    db: AsyncSession,
    organization_id: uuid.UUID,
    *,
    vendor: str,
    product: str,
    name: str = "",
    software_type: str = "application",
    description: str = "",
    cpe: str = "",
    purl: str = "",
    version: str = "",
) -> dict[str, Any]:
    """Type a software by hand (``origin`` ``manual``); 409 when the vendor and product already exist.

    *version*, when given, is the version the organization runs: stored as a
    version of the software (a hand-typed software keeps its typed versions).
    """
    product = _cut(product, 200)
    if not product:
        raise SoftwareError("product is required")
    vendor = _cut(vendor, 200)
    key = (_key(vendor)[:200], _key(product)[:200])
    taken = (
        await db.execute(
            select(OrganizationSoftware.id).where(
                OrganizationSoftware.organization_id == organization_id,
                OrganizationSoftware.vendor_key == key[0],
                OrganizationSoftware.product_key == key[1],
            )
        )
    ).scalar_one_or_none()
    if taken is not None:
        raise SoftwareError("A software with this vendor and product already exists", 409)
    soft = OrganizationSoftware(
        id=uuid.uuid4(),
        organization_id=organization_id,
        vendor=vendor,
        product=product,
        vendor_key=key[0],
        product_key=key[1],
        name=_cut(name, 300) or _cut(display_name(vendor, product), 300),
        software_type=_check_type(software_type),
        description=_cut(description, 2000),
        cpe_override=_check_cpe(cpe),
        purl=_cut(purl, 500),
        origin="manual",
        sources=["manual"],
    )
    soft.name_key = _key(soft.name)[:300]
    _resolve(soft)
    db.add(soft)
    await db.flush()
    typed_version = _cut(version, 100)
    if typed_version and typed_version != identity.UNKNOWN_VERSION:
        db.add(
            OrganizationSoftwareVersion(
                id=uuid.uuid4(),
                organization_id=organization_id,
                software_id=soft.id,
                version=typed_version,
                cpe=_cut(version_cpe(soft.cpe, typed_version), 300),
            )
        )
        await db.flush()
    return _software_dict(soft)


async def software_keys(db: AsyncSession, organization_id: uuid.UUID) -> set[Key]:
    """The ``(vendor, product)`` keys of every software of *organization_id* (one read)."""
    soft = OrganizationSoftware
    rows = await db.execute(select(soft.vendor_key, soft.product_key).where(soft.organization_id == organization_id))
    return {(str(vendor), str(product)) for vendor, product in rows.all()}


#: The fields of a hand-typed software an administrator may change; a synced one takes ``cpe_override`` only.
EDITABLE_FIELDS: tuple[str, ...] = ("name", "type", "description", "purl", "cpe_override")


async def update_software(
    db: AsyncSession, organization_id: uuid.UUID, software_id: uuid.UUID, changes: Mapping[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Change a software; answers it and the fields changed (``{field: [before, after]}``).

    A synced software is read-only but for its CPE override (422 for any other
    field): its name, type and versions come from its sources.
    """
    soft = await _software(db, organization_id, software_id)
    unknown = set(changes) - set(EDITABLE_FIELDS)
    if unknown:
        raise SoftwareError(f"Unknown field(s): {', '.join(sorted(unknown))}")
    if soft.origin != "manual" and set(changes) - {"cpe_override"}:
        raise SoftwareError("A synced software is read-only; only its CPE override can be set")
    before = _software_dict(soft)
    if "name" in changes:
        soft.name = _cut(changes["name"], 300) or _cut(display_name(soft.vendor, soft.product), 300)
        soft.name_key = _key(soft.name)[:300]
    if "type" in changes:
        soft.software_type = _check_type(str(changes["type"] or ""))
    if "description" in changes:
        soft.description = _cut(changes["description"], 2000)
    if "purl" in changes:
        soft.purl = _cut(changes["purl"], 500)
    if "cpe_override" in changes:
        soft.cpe_override = _check_cpe(str(changes["cpe_override"] or ""))
        if soft.origin == "manual" or soft.cpe_override:
            _resolve(soft)
        else:
            # The override is cleared on a synced software: the next rebuild resolves its CPE from its sources.
            soft.cpe, soft.cpe_state = "", "unresolved"
    await db.flush()
    after = _software_dict(soft)
    changed = {f: [before[f], after[f]] for f in EDITABLE_FIELDS if before[f] != after[f]}
    return after, changed


async def _hand_documents(db: AsyncSession, organization_id: uuid.UUID) -> list[Any]:
    from app.models.organization_asset import OrganizationAssetSbom
    from app.services.organization_asset_store import HAND_SOFTWARE_FORMAT

    return list(
        (
            await db.execute(
                select(OrganizationAssetSbom).where(
                    OrganizationAssetSbom.organization_id == organization_id,
                    OrganizationAssetSbom.format == HAND_SOFTWARE_FORMAT,
                )
            )
        )
        .scalars()
        .all()
    )


def _is_component_of(component: Mapping[str, Any], soft: OrganizationSoftware) -> bool:
    return _key(component.get("vendor")) == soft.vendor_key and _key(component.get("product")) == soft.product_key


async def delete_software(db: AsyncSession, organization_id: uuid.UUID, software_id: uuid.UUID) -> dict[str, Any]:
    """Delete a hand-typed software and its hand attachments (422 for a synced one); the caller rebuilds."""
    soft = await _software(db, organization_id, software_id)
    if soft.origin != "manual":
        raise SoftwareError("A synced software cannot be deleted; it goes when no source reports it")
    gone = _software_dict(soft)
    detached = 0
    for doc in await _hand_documents(db, organization_id):
        kept = [c for c in doc.components or [] if not _is_component_of(c, soft)]
        detached += len(doc.components or []) - len(kept)
        if kept:
            doc.components = kept
        else:
            await db.delete(doc)
    await db.delete(soft)
    await db.flush()
    return {**gone, "detached": detached}


async def _anchor(db: AsyncSession, organization_id: uuid.UUID, asset_id: uuid.UUID) -> tuple[str, str]:
    from app.models.organization_asset import OrganizationAssetSource

    anchor = (
        await db.execute(
            select(OrganizationAssetSource.source, OrganizationAssetSource.external_id)
            .join(OrganizationAsset, OrganizationAsset.id == OrganizationAssetSource.asset_id)
            .where(OrganizationAsset.organization_id == organization_id, OrganizationAsset.id == asset_id)
            .order_by(OrganizationAssetSource.source, OrganizationAssetSource.external_id)
            .limit(1)
        )
    ).first()
    if anchor is None:
        raise SoftwareError("Asset not found", 404)
    return str(anchor[0]), str(anchor[1])


async def attach_software(
    db: AsyncSession,
    organization_id: uuid.UUID,
    software_id: uuid.UUID,
    asset_id: uuid.UUID,
    *,
    version: str = "",
    author_user_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """Attach a hand-typed software to an asset at *version* (``unknown`` when empty); the caller rebuilds.

    The attachment is a component of the asset's hand document, anchored on its
    first source record, so it follows the asset through every rebuild and merge.
    Attaching the same version again changes nothing; a synced software is
    attached by its sources only (422).
    """
    from datetime import UTC
    from datetime import datetime as _dt

    from app.models.organization_asset import OrganizationAssetSbom
    from app.services.organization_asset_store import HAND_SOFTWARE_FORMAT

    soft = await _software(db, organization_id, software_id)
    if soft.origin != "manual":
        raise SoftwareError("A synced software is attached by its sources; type the software by hand to attach it")
    source, external_id = await _anchor(db, organization_id, asset_id)
    version = _cut(version, 100) or identity.UNKNOWN_VERSION
    doc = (
        await db.execute(
            select(OrganizationAssetSbom).where(
                OrganizationAssetSbom.organization_id == organization_id,
                OrganizationAssetSbom.anchor_source == source,
                OrganizationAssetSbom.anchor_external_id == external_id,
                OrganizationAssetSbom.document_key == HAND_SOFTWARE_FORMAT,
            )
        )
    ).scalar_one_or_none()
    if doc is None:
        doc = OrganizationAssetSbom(
            organization_id=organization_id,
            anchor_source=source,
            anchor_external_id=external_id,
            document_key=HAND_SOFTWARE_FORMAT,
            format=HAND_SOFTWARE_FORMAT,
            file_name="",
            components=[],
        )
        db.add(doc)
    components = list(doc.components or [])
    entry = {
        "vendor": soft.vendor,
        "product": soft.product,
        "version": version,
        "cpe": version_cpe(soft.cpe, version),
        "purl": soft.purl,
        "bundled_in": "",
    }
    added = not any(_is_component_of(c, soft) and str(c.get("version")) == version for c in components)
    if added:
        doc.components = [*components, entry]
        doc.author_user_id = author_user_id
        doc.imported_at = _dt.now(UTC)
    await db.flush()
    return {"software_id": str(soft.id), "asset_id": str(asset_id), "version": version, "added": added}


async def detach_software(
    db: AsyncSession, organization_id: uuid.UUID, software_id: uuid.UUID, asset_id: uuid.UUID, *, version: str = ""
) -> int:
    """Remove a hand attachment from an asset (every version when *version* is empty); answers how many went.

    404 when the asset holds no hand attachment of that software; the caller rebuilds.
    """
    from app.services.organization_asset_store import HAND_SOFTWARE_FORMAT

    soft = await _software(db, organization_id, software_id)
    source, external_id = await _anchor(db, organization_id, asset_id)
    removed = 0
    for doc in await _hand_documents(db, organization_id):
        if (doc.anchor_source, doc.anchor_external_id, doc.document_key) != (source, external_id, HAND_SOFTWARE_FORMAT):
            continue
        kept = [
            c
            for c in doc.components or []
            if not (_is_component_of(c, soft) and (not version or str(c.get("version")) == version))
        ]
        removed += len(doc.components or []) - len(kept)
        if kept:
            doc.components = kept
        else:
            await db.delete(doc)
    if not removed:
        raise SoftwareError("This asset holds no hand attachment of this software", 404)
    await db.flush()
    return removed


# ── CSV export ────────────────────────────────────────────────────────

#: The columns of the software CSV, in order.
CSV_COLUMNS: tuple[str, ...] = (
    "name",
    "vendor",
    "product",
    "type",
    "cpe",
    "cpe_state",
    "purl",
    "origin",
    "sources",
    "version_count",
    "asset_count",
    "has_unknown_version",
    "first_seen",
    "last_seen",
)
#: The most rows one export writes.
MAX_CSV_ROWS = 50_000


def _csv_cell(value: Any) -> str:
    """A cell a spreadsheet never runs as a formula (``=``, ``+``, ``-``, ``@``, tab and CR are prefixed with ``'``)."""
    if isinstance(value, list):
        value = "; ".join(str(v) for v in value)
    text_value = "" if value is None else str(value)
    return f"'{text_value}" if text_value[:1] in ("=", "+", "-", "@", "\t", "\r") else text_value


async def software_csv(db: AsyncSession, organization_id: uuid.UUID, **filters: Any) -> str:
    """The software list as CSV (the filters of :func:`list_software`), at most :data:`MAX_CSV_ROWS` rows."""
    import csv
    import io

    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)
    offset = 0
    while offset < MAX_CSV_ROWS:
        page = await list_software(db, organization_id, offset=offset, limit=MAX_PAGE, **filters)
        for item in page.items:
            writer.writerow([_csv_cell(item[c]) for c in CSV_COLUMNS])
        offset += len(page.items)
        if not page.items or offset >= page.total:
            break
    return out.getvalue()
