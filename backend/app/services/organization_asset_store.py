# SPDX-License-Identifier: Apache-2.0

"""The canonical asset store of the organization context (#1178, S2): build and read.

The identity engine (``organization_asset_identity``, S1) is pure; this module
persists its answer in ``models/organization_asset.py`` and answers the reads:

- :func:`rebuild_from_profile` turns the ``assets_exposure`` rows of an
  organization's current profile into canonical assets (one per real thing,
  with every source record, every field's per-source values and the merged
  software), replacing what the store held for that organization. The profile
  caps its rows at 5,000, so a rebuild is bounded.
- :func:`ensure_built` rebuilds only when the store was built from an older
  profile version (or never): the migration backfills nothing, the first read
  after a save builds the store, so an existing profile keeps working.
- :func:`list_assets`, :func:`get_asset` and :func:`assets_of_software` are the
  paginated reads of the API (``GET /api/v1/organization-context/assets``).

Writes from syncs and agent tools through the store, and administrator merge
and split, are the next slice (S3); until then the profile stays the source of
the rows and the store is its canonical view.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.organization_asset import OrganizationAsset, OrganizationAssetSoftware, OrganizationAssetSource
from app.models.organization_context import OrganizationContextVersion
from app.services import organization_asset_identity as identity

#: The most rows a page answers; a larger ``limit`` is clamped.
MAX_PAGE = 200


def _key(value: str) -> str:
    return " ".join(str(value or "").split()).casefold()


def _seen(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _cut(value: Any, size: int) -> str:
    return str(value or "").strip()[:size]


def canonical_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    precedence: Mapping[str, Sequence[str]] | None = None,
) -> list[dict[str, Any]]:
    """The canonical assets of profile *rows*: what :func:`rebuild_from_profile` writes (pure, testable).

    Each answer holds ``canonical_key``, ``name``, ``fields`` (field ->
    ``FieldValue.as_dict()``), ``merged_on``, ``possible_matches``,
    ``sources`` (one per record) and ``software`` (``merge_software``).
    """
    records = [identity.section_record("assets", row, index=i) for i, row in enumerate(rows)]
    out: list[dict[str, Any]] = []
    for thing in identity.match_records(records):
        fields = identity.resolve_fields(thing.records, precedence=precedence)
        name = fields.get("name")
        out.append(
            {
                "canonical_key": thing.id,
                "name": str(name.value if name else "")[:300],
                "fields": {k: v.as_dict() for k, v in fields.items()},
                "merged_on": thing.merged_on,
                "possible_matches": thing.possible_matches,
                "explanation": thing.explanation(),
                "sources": [
                    {
                        "source": r.source,
                        "external_id": r.external_id,
                        "identifiers": {k: [str(v) for v in vals if v] for k, vals in r.identifiers.items() if vals},
                        "fields": r.fields,
                        "last_seen": r.last_seen,
                    }
                    for r in thing.records
                ],
                "software": identity.merge_software(thing.records),
            }
        )
    return out


async def _current(db: AsyncSession, organization_id: uuid.UUID | None) -> OrganizationContextVersion | None:
    from app.services.organization_context import current_version

    return await current_version(db, organization_id)


async def rebuild_from_profile(db: AsyncSession, organization_id: uuid.UUID | None = None) -> int:
    """Replace the store of *organization_id* (the primary when ``None``) with its profile's canonical assets.

    Answers the number of canonical assets written (the caller commits). No
    profile yet: the store is emptied and 0 is answered.
    """
    version = await _current(db, organization_id)
    if version is None:
        if organization_id is not None:
            await db.execute(delete(OrganizationAsset).where(OrganizationAsset.organization_id == organization_id))
        return 0
    org = version.organization_id
    # Two first reads racing would both rebuild: the second waits for the first's transaction.
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"organization_assets:{org}"})
    rows = [row for row in (version.profile or {}).get("assets_exposure") or [] if isinstance(row, Mapping)]
    things = canonical_rows(rows)
    # Children go with their asset (ON DELETE CASCADE).
    await db.execute(delete(OrganizationAsset).where(OrganizationAsset.organization_id == org))
    for thing in things:
        asset = OrganizationAsset(
            id=uuid.uuid4(),
            organization_id=org,
            canonical_key=thing["canonical_key"],
            name=thing["name"],
            name_key=_key(thing["name"])[:300],
            fields=_jsonable(thing["fields"]),
            merged_on=thing["merged_on"],
            possible_matches=thing["possible_matches"],
            profile_version=int(version.version),
        )
        db.add(asset)
        for rec in thing["sources"]:
            db.add(
                OrganizationAssetSource(
                    organization_id=org,
                    asset_id=asset.id,
                    source=_cut(rec["source"], 80),
                    external_id=_cut(rec["external_id"], 200),
                    identifiers=rec["identifiers"],
                    fields=_jsonable(rec["fields"]),
                    last_seen_at=rec["last_seen"],
                )
            )
        for soft in thing["software"]:
            db.add(
                OrganizationAssetSoftware(
                    organization_id=org,
                    asset_id=asset.id,
                    vendor=_cut(soft["vendor"], 200),
                    product=_cut(soft["product"], 200),
                    vendor_key=_key(soft["vendor"])[:200],
                    product_key=_key(soft["product"])[:200],
                    version=_cut(soft["version"], 100) or identity.UNKNOWN_VERSION,
                    cpe=_cut(soft["cpe"], 300),
                    purl=_cut(soft["purl"], 500),
                    install_date=_cut(soft["install_date"], 40),
                    first_seen_at=_seen(soft["first_seen"]),
                    last_seen_at=_seen(soft["last_seen"]),
                    sources=list(soft["sources"]),
                )
            )
    await db.flush()
    return len(things)


def _jsonable(value: Any) -> Any:
    """*value* with datetimes as ISO strings, for a JSONB column."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


async def ensure_built(db: AsyncSession, organization_id: uuid.UUID | None = None) -> uuid.UUID | None:
    """Build the store when it lags the current profile; answer the organization id (``None``: no profile)."""
    version = await _current(db, organization_id)
    if version is None:
        return None
    org: uuid.UUID = version.organization_id
    built = (
        await db.execute(
            select(func.max(OrganizationAsset.profile_version)).where(OrganizationAsset.organization_id == org)
        )
    ).scalar()
    has_rows = bool((version.profile or {}).get("assets_exposure"))
    if (built or 0) != int(version.version) and (has_rows or built is not None):
        await rebuild_from_profile(db, org)
    return org


@dataclass
class Page:
    items: list[dict[str, Any]]
    total: int
    offset: int
    limit: int


def _limit(limit: int) -> int:
    return max(1, min(int(limit or 50), MAX_PAGE))


def _summary(asset: OrganizationAsset, sources: list[str], software_count: int) -> dict[str, Any]:
    fields = asset.fields or {}

    def value(name: str) -> Any:
        entry = fields.get(name)
        return entry.get("value") if isinstance(entry, Mapping) else None

    return {
        "id": str(asset.id),
        "name": asset.name,
        "category": value("category"),
        "subcategory": value("subcategory"),
        "criticality": value("criticality"),
        "internet_facing": value("internet_facing"),
        "owner": value("owner"),
        "os_family": value("os_family"),
        "os_version": value("os_version"),
        "sources": sources,
        "software_count": software_count,
        "merged_on": asset.merged_on or [],
    }


async def list_assets(
    db: AsyncSession,
    organization_id: uuid.UUID,
    *,
    q: str = "",
    offset: int = 0,
    limit: int = 50,
) -> Page:
    """The canonical assets of *organization_id*, by name, filtered on a name substring *q*."""
    limit, offset = _limit(limit), max(0, int(offset or 0))
    where = [OrganizationAsset.organization_id == organization_id]
    if q.strip():
        where.append(OrganizationAsset.name_key.contains(_key(q)))
    total = (await db.execute(select(func.count()).select_from(OrganizationAsset).where(*where))).scalar() or 0
    assets = (
        (
            await db.execute(
                select(OrganizationAsset)
                .where(*where)
                .order_by(OrganizationAsset.name_key, OrganizationAsset.canonical_key)
                .offset(offset)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    ids = [a.id for a in assets]
    sources: dict[uuid.UUID, list[str]] = {}
    counts: dict[uuid.UUID, int] = {}
    if ids:
        for asset_id, source in (
            await db.execute(
                select(OrganizationAssetSource.asset_id, OrganizationAssetSource.source)
                .where(OrganizationAssetSource.asset_id.in_(ids))
                .order_by(OrganizationAssetSource.source)
            )
        ).all():
            names = sources.setdefault(asset_id, [])
            if source not in names:
                names.append(source)
        for asset_id, count in (
            await db.execute(
                select(OrganizationAssetSoftware.asset_id, func.count())
                .where(OrganizationAssetSoftware.asset_id.in_(ids))
                .group_by(OrganizationAssetSoftware.asset_id)
            )
        ).all():
            counts[asset_id] = int(count)
    return Page(
        items=[_summary(a, sources.get(a.id, []), int(counts.get(a.id, 0))) for a in assets],
        total=int(total),
        offset=offset,
        limit=limit,
    )


def _software_dict(row: OrganizationAssetSoftware) -> dict[str, Any]:
    return {
        "vendor": row.vendor,
        "product": row.product,
        "version": row.version,
        "cpe": row.cpe,
        "purl": row.purl,
        "install_date": row.install_date or None,
        "first_seen": row.first_seen_at.isoformat() if row.first_seen_at else None,
        "last_seen": row.last_seen_at.isoformat() if row.last_seen_at else None,
        "sources": list(row.sources or []),
    }


async def get_asset(
    db: AsyncSession,
    organization_id: uuid.UUID,
    asset_id: uuid.UUID,
    *,
    software_offset: int = 0,
    software_limit: int = 100,
) -> dict[str, Any] | None:
    """One asset with its fields and their origins, its source records and a page of its software."""
    asset = (
        await db.execute(
            select(OrganizationAsset).where(
                OrganizationAsset.organization_id == organization_id, OrganizationAsset.id == asset_id
            )
        )
    ).scalar_one_or_none()
    if asset is None:
        return None
    records = (
        (
            await db.execute(
                select(OrganizationAssetSource)
                .where(OrganizationAssetSource.asset_id == asset.id)
                .order_by(OrganizationAssetSource.source, OrganizationAssetSource.external_id)
            )
        )
        .scalars()
        .all()
    )
    limit, offset = _limit(software_limit), max(0, int(software_offset or 0))
    soft_where = OrganizationAssetSoftware.asset_id == asset.id
    soft_total = (
        await db.execute(select(func.count()).select_from(OrganizationAssetSoftware).where(soft_where))
    ).scalar() or 0
    software = (
        (
            await db.execute(
                select(OrganizationAssetSoftware)
                .where(soft_where)
                .order_by(
                    OrganizationAssetSoftware.vendor_key,
                    OrganizationAssetSoftware.product_key,
                    OrganizationAssetSoftware.version,
                )
                .offset(offset)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    names = list(dict.fromkeys(r.source for r in records))
    return {
        **_summary(asset, names, int(soft_total)),
        "fields": asset.fields or {},
        "explanation": identity.Canonical(
            id=asset.canonical_key, records=[], merged_on=list(asset.merged_on or [])
        ).explanation(),
        "possible_matches": asset.possible_matches or [],
        "records": [
            {
                "source": r.source,
                "external_id": r.external_id,
                "identifiers": r.identifiers or {},
                "fields": r.fields or {},
                "last_seen": r.last_seen_at.isoformat() if r.last_seen_at else None,
            }
            for r in records
        ],
        "software": {
            "items": [_software_dict(s) for s in software],
            "total": int(soft_total),
            "offset": offset,
            "limit": limit,
        },
    }


async def assets_of_software(
    db: AsyncSession,
    organization_id: uuid.UUID,
    *,
    product: str,
    vendor: str = "",
    version: str = "",
    offset: int = 0,
    limit: int = 50,
) -> Page:
    """The assets running *product* (and *vendor*, *version* when given), with the version each runs."""
    limit, offset = _limit(limit), max(0, int(offset or 0))
    where = [
        OrganizationAssetSoftware.organization_id == organization_id,
        OrganizationAssetSoftware.product_key == _key(product),
    ]
    if vendor.strip():
        where.append(OrganizationAssetSoftware.vendor_key == _key(vendor))
    if version.strip():
        where.append(OrganizationAssetSoftware.version == version.strip())
    total = (await db.execute(select(func.count()).select_from(OrganizationAssetSoftware).where(*where))).scalar() or 0
    rows = (
        await db.execute(
            select(OrganizationAssetSoftware, OrganizationAsset.name)
            .join(OrganizationAsset, OrganizationAsset.id == OrganizationAssetSoftware.asset_id)
            .where(*where)
            .order_by(OrganizationAsset.name_key, OrganizationAssetSoftware.version, OrganizationAsset.id)
            .offset(offset)
            .limit(limit)
        )
    ).all()
    return Page(
        items=[{"asset_id": str(s.asset_id), "asset_name": name, **_software_dict(s)} for s, name in rows],
        total=int(total),
        offset=offset,
        limit=limit,
    )
