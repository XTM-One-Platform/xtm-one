# SPDX-License-Identifier: Apache-2.0

"""The inventory query surface agents read (#2443, slice S1).

An organization's inventory runs to thousands of assets and software: no agent
reads it from its prompt or pages a category end to end. Every function here
answers ONE question with SQL paging, filtering, counting and grouping done by
PostgreSQL over ``organization_asset_store`` / ``organization_software_store``
tables, never a full load into Python:

- :func:`search_assets` filters and pages the canonical assets.
- :func:`get_asset` reads one asset (by id or 8+ hex prefix) with its software page,
  its groups and the primary assets it supports.
- :func:`lookup_assets` resolves hostnames, IPs, FQDNs, MACs and ``source:id`` references.
- :func:`search_software` / :func:`get_software` page the Software entities.
- :func:`find_assets_running` answers "which assets run X (in this version range),
  directly or through a dependency".
- :func:`aggregate_inventory` counts assets or software by one field.
- :func:`list_asset_groups`, :func:`list_primary_assets` and
  :func:`list_inventory_changes` read groups, primary assets and the change log.

The renderers (:func:`render`) quarantine the stored values as untrusted data:
an asset's name or a software's description is third-party text.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import String, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.organization_asset import (
    OrganizationAsset,
    OrganizationAssetSoftware,
    OrganizationSoftware,
    OrganizationSoftwareDependency,
    OrganizationSoftwareVersion,
)
from app.models.organization_asset_record import (
    OrganizationAssetGroup,
    OrganizationAssetGroupMember,
    OrganizationAssetRecord,
    OrganizationPrimaryAssetLink,
)
from app.services import organization_asset_store as store
from app.services import organization_software_store as software_store
from app.services.ctem_fixed_version import compare_versions

#: Rows one page answers by default and at most.
DEFAULT_PAGE = 50
MAX_PAGE = 200
#: How many distinct values one aggregation answers at most (the rest are summed as ``other``).
MAX_BUCKETS = 100
#: How many installed versions one range filter compares at most; a product with more is
#: answered with the versions compared and ``truncated`` true, never silently.
MAX_RANGE_VERSIONS = 5000
#: How far a dependency walk goes (versions reached).
MAX_WALK = software_store.MAX_WALK

#: Fields an asset aggregation groups by: the asset's merged fields, plus ``source``.
ASSET_GROUP_FIELDS: tuple[str, ...] = (
    "category",
    "subcategory",
    "criticality",
    "internet_facing",
    "os_family",
    "environment",
    "owner",
    "source",
)
#: Fields a software aggregation groups by.
SOFTWARE_GROUP_FIELDS: tuple[str, ...] = ("software_type", "vendor", "origin", "cpe_state")


class QueryError(ValueError):
    """A query the inventory cannot answer as asked; the message says how to ask it."""


def _limit(limit: object) -> int:
    try:
        value = int(str(limit or DEFAULT_PAGE))
    except ValueError:
        value = DEFAULT_PAGE
    return max(1, min(value, MAX_PAGE))


def _offset(offset: object) -> int:
    try:
        return max(0, int(str(offset or 0)))
    except ValueError:
        return 0


def _key(value: object) -> str:
    return " ".join(str(value or "").split()).casefold()


def page(items: list[dict[str, Any]], *, total: int, offset: int, limit: int, **extra: Any) -> dict[str, Any]:
    """A page as every function answers it: the rows, the position and the next offset (``None``: the end)."""
    nxt = offset + len(items)
    return {
        "items": items,
        "total": int(total),
        "offset": offset,
        "limit": limit,
        "next_offset": nxt if nxt < total else None,
        **extra,
    }


# ── Assets ────────────────────────────────────────────────────────────────


def _filters(
    *,
    category: Sequence[str] | str = (),
    criticality: Sequence[str] | str = (),
    internet_facing: Sequence[str] | str = (),
    os_family: Sequence[str] | str = (),
    subcategory: Sequence[str] | str = (),
    source: Sequence[str] | str = (),
    group: Sequence[str] | str = (),
) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for name, value in (
        ("category", category),
        ("criticality", criticality),
        ("internet_facing", internet_facing),
        ("os_family", os_family),
        ("subcategory", subcategory),
        ("source", source),
        ("group", group),
    ):
        values = [value] if isinstance(value, str) else list(value or ())
        values = [str(v).strip() for v in values if str(v or "").strip()]
        if values:
            out[name] = values
    return out


async def search_assets(
    db: AsyncSession,
    organization_id: uuid.UUID,
    *,
    query: str = "",
    offset: object = 0,
    limit: object = DEFAULT_PAGE,
    **filters: Any,
) -> dict[str, Any]:
    """The canonical assets matching *query* (name, OS, owner) and *filters*, one SQL page.

    A filter that matches nothing answers ``held``: the values that field does hold.
    """
    wanted = _filters(**filters)
    size, start = _limit(limit), _offset(offset)
    found = await store.list_assets(db, organization_id, q=str(query or ""), offset=start, limit=size, filters=wanted)
    held = await store.held_values(db, organization_id, wanted) if not found.total and wanted else None
    return page(list(found.items), total=found.total, offset=start, limit=size, **({"held": held} if held else {}))


async def get_asset(
    db: AsyncSession,
    organization_id: uuid.UUID,
    asset: str,
    *,
    software_offset: object = 0,
    software_limit: object = DEFAULT_PAGE,
) -> dict[str, Any] | None:
    """One asset by id, 8+ hex prefix or exact identifier, with its software page, groups and primary assets."""
    from app.services.ctem_primary_assets import asset_primary_assets

    asset_id = await _resolve_asset(db, organization_id, asset)
    if asset_id is None:
        return None
    detail = await store.get_asset(
        db,
        organization_id,
        asset_id,
        software_offset=_offset(software_offset),
        software_limit=_limit(software_limit),
    )
    if detail is None:
        return None
    detail["groups"] = await _asset_groups(db, organization_id, asset_id)
    detail["primary_assets"] = await asset_primary_assets(db, organization_id, asset_id)
    return detail


async def _resolve_asset(db: AsyncSession, organization_id: uuid.UUID, ref: str) -> uuid.UUID | None:
    text = str(ref or "").strip()
    if not text:
        return None
    try:
        return uuid.UUID(text)
    except ValueError:
        pass
    prefix = store.id_prefix(text)
    if prefix is not None:
        rows = (
            await db.execute(
                select(OrganizationAsset.id)
                .where(
                    OrganizationAsset.organization_id == organization_id,
                    cast(OrganizationAsset.id, String).like(f"{prefix}%"),
                )
                .limit(2)
            )
        ).all()
        if len(rows) == 1:
            return uuid.UUID(str(rows[0][0]))
    found = await store.find_assets_by_identifier(db, organization_id, [text])
    hit = found.get(text)
    return uuid.UUID(hit["id"]) if hit else None


async def _asset_groups(db: AsyncSession, organization_id: uuid.UUID, asset_id: uuid.UUID) -> list[dict[str, str]]:
    grp, member, rec = OrganizationAssetGroup, OrganizationAssetGroupMember, OrganizationAssetRecord
    rows = (
        await db.execute(
            select(grp.id, grp.name, grp.source)
            .join(member, member.group_id == grp.id)
            .join(rec, rec.id == member.record_id)
            .where(grp.organization_id == organization_id, rec.asset_id == asset_id)
            .distinct()
            .order_by(grp.name)
        )
    ).all()
    return [{"id": str(gid), "name": str(name), "source": str(source)} for gid, name, source in rows]


async def lookup_assets(db: AsyncSession, organization_id: uuid.UUID, values: Sequence[str]) -> dict[str, Any]:
    """The asset each value names (hostname, FQDN, IP, MAC, ``source:external_id`` or exact name)."""
    asked = [str(v).strip() for v in values if str(v or "").strip()][: store.MAX_LOOKUP_KEYS]
    found = await store.find_assets_by_identifier(db, organization_id, asked)
    return {"found": found, "not_found": [v for v in asked if v not in found]}


# ── Software ──────────────────────────────────────────────────────────────


async def search_software(
    db: AsyncSession,
    organization_id: uuid.UUID,
    *,
    query: str = "",
    software_type: Sequence[str] | str = (),
    vendor: str = "",
    origin: Sequence[str] | str = (),
    cpe_state: Sequence[str] | str = (),
    unknown_version: bool | None = None,
    offset: object = 0,
    limit: object = DEFAULT_PAGE,
) -> dict[str, Any]:
    """The Software entities matching *query* (name, vendor, product, CPE) and the filters, one SQL page."""
    size, start = _limit(limit), _offset(offset)
    found = await software_store.list_software(
        db,
        organization_id,
        q=str(query or ""),
        software_type=software_type or "",
        vendor=str(vendor or ""),
        origin=origin or "",
        cpe_state=cpe_state or "",
        unknown_version=unknown_version,
        offset=start,
        limit=size,
    )
    return page(list(found.items), total=found.total, offset=start, limit=size)


async def get_software(
    db: AsyncSession,
    organization_id: uuid.UUID,
    software: str,
    *,
    installations_offset: object = 0,
    installations_limit: object = DEFAULT_PAGE,
) -> dict[str, Any] | None:
    """One software by id or exact name (``vendor product``), with versions, a page of installations and edges."""
    software_id = await _resolve_software(db, organization_id, software)
    if software_id is None:
        return None
    return await software_store.get_software(
        db,
        organization_id,
        software_id,
        installations_offset=_offset(installations_offset),
        installations_limit=_limit(installations_limit),
    )


async def _resolve_software(db: AsyncSession, organization_id: uuid.UUID, ref: str) -> uuid.UUID | None:
    text = str(ref or "").strip()
    if not text:
        return None
    try:
        return uuid.UUID(text)
    except ValueError:
        pass
    soft = OrganizationSoftware
    rows = (
        await db.execute(
            select(soft.id)
            .where(soft.organization_id == organization_id, soft.name_key == _key(text))
            .order_by(soft.id)
            .limit(2)
        )
    ).all()
    if len(rows) == 1:
        return uuid.UUID(str(rows[0][0]))
    rows = (
        await db.execute(
            select(soft.id)
            .where(soft.organization_id == organization_id, soft.product_key == _key(text))
            .order_by(soft.id)
            .limit(2)
        )
    ).all()
    return rows[0][0] if len(rows) == 1 else None


def version_in_range(
    version: str,
    *,
    min_version: str = "",
    max_version: str = "",
    min_inclusive: bool = True,
    max_inclusive: bool = False,
) -> bool | None:
    """Whether *version* is within the bounds; ``None`` when a bound or the version is not comparable."""
    if min_version:
        cmp = compare_versions(version, min_version)
        if cmp is None:
            return None
        if cmp < 0 or (cmp == 0 and not min_inclusive):
            return False
    if max_version:
        cmp = compare_versions(version, max_version)
        if cmp is None:
            return None
        if cmp > 0 or (cmp == 0 and not max_inclusive):
            return False
    return True


async def find_assets_running(
    db: AsyncSession,
    organization_id: uuid.UUID,
    *,
    product: str = "",
    vendor: str = "",
    cpe: str = "",
    version: str = "",
    min_version: str = "",
    max_version: str = "",
    min_inclusive: bool = True,
    max_inclusive: bool = False,
    through_dependencies: bool = True,
    offset: object = 0,
    limit: object = DEFAULT_PAGE,
) -> dict[str, Any]:
    """The assets running a software, optionally in a version range, directly or through a dependency.

    The software is named by *product* (and *vendor*) or by a product-level *cpe*. *version*
    is an exact version; *min_version* / *max_version* a range (``1.2 <= v < 1.4`` by default).
    Version comparison is semantic (``1.10`` > ``1.9``); a version that is not comparable
    (``unknown``, a build tag) is listed in ``uncomparable`` and never counted inside a range.
    With *through_dependencies*, an asset whose installed software needs a matching version
    (an SBOM dependency, transitive) is answered too, ``via`` naming what it runs.
    """
    if not (product.strip() or cpe.strip()):
        raise QueryError("name the software: product (and vendor), or a cpe")
    ver, soft = OrganizationSoftwareVersion, OrganizationSoftware
    where = [ver.organization_id == organization_id, soft.organization_id == organization_id]
    if cpe.strip():
        where.append(soft.cpe == software_store.product_cpe(cpe.strip()))
    else:
        where.append(soft.product_key == _key(product))
        if vendor.strip():
            where.append(soft.vendor_key == _key(vendor))
    candidates = (
        await db.execute(
            select(ver.id, ver.version, soft.name)
            .join(soft, soft.id == ver.software_id)
            .where(*where)
            .order_by(ver.id)
            .limit(MAX_RANGE_VERSIONS + 1)
        )
    ).all()
    truncated = len(candidates) > MAX_RANGE_VERSIONS
    candidates = candidates[:MAX_RANGE_VERSIONS]
    matched: list[uuid.UUID] = []
    uncomparable: list[str] = []
    ranged = bool(min_version.strip() or max_version.strip())
    for version_id, value, _name in candidates:
        if version.strip():
            if compare_versions(value, version.strip()) == 0 or value == version.strip():
                matched.append(version_id)
            continue
        if not ranged:
            matched.append(version_id)
            continue
        inside = version_in_range(
            value,
            min_version=min_version.strip(),
            max_version=max_version.strip(),
            min_inclusive=min_inclusive,
            max_inclusive=max_inclusive,
        )
        if inside is None:
            uncomparable.append(value)
        elif inside:
            matched.append(version_id)
    size, start = _limit(limit), _offset(offset)
    extra = {
        "versions_matched": len(matched),
        "uncomparable": sorted(set(uncomparable))[:50],
        "truncated": truncated,
    }
    if not matched:
        return page([], total=0, offset=start, limit=size, **extra)
    # Which matched version each reached version leads back to: the reverse dependency walk.
    reach: dict[uuid.UUID, uuid.UUID] = {v: v for v in matched}
    frontier = list(matched)
    dep = OrganizationSoftwareDependency
    while through_dependencies and frontier and len(reach) < MAX_WALK:
        edges = (
            await db.execute(
                select(dep.version_id, dep.depends_on_version_id).where(dep.depends_on_version_id.in_(frontier))
            )
        ).all()
        frontier = []
        for user, target in edges:
            if user not in reach:
                reach[user] = reach[target]
                frontier.append(user)
    inst = OrganizationAssetSoftware
    base = [inst.organization_id == organization_id, inst.software_version_id.in_(list(reach))]
    total = (await db.execute(select(func.count(func.distinct(inst.asset_id))).where(*base))).scalar() or 0
    asset_ids = [
        row[0]
        for row in (
            await db.execute(
                select(inst.asset_id, func.min(OrganizationAsset.name_key).label("name_key"))
                .join(OrganizationAsset, OrganizationAsset.id == inst.asset_id)
                .where(*base)
                .group_by(inst.asset_id)
                .order_by("name_key", inst.asset_id)
                .offset(start)
                .limit(size)
            )
        ).all()
    ]
    rows = (
        await db.execute(
            select(inst, OrganizationAsset.name)
            .join(OrganizationAsset, OrganizationAsset.id == inst.asset_id)
            .where(*base, inst.asset_id.in_(asset_ids))
        )
    ).all()
    by_asset: dict[uuid.UUID, dict[str, Any]] = {}
    for installed, name in rows:
        entry = by_asset.setdefault(
            installed.asset_id, {"asset_id": str(installed.asset_id), "asset_name": name, "runs": []}
        )
        direct = installed.software_version_id in matched
        entry["runs"].append(
            {
                "product": installed.product,
                "version": installed.version,
                "via": None if direct else f"{installed.product} {installed.version}",
            }
        )
    items = [by_asset[a] for a in asset_ids if a in by_asset]
    return page(items, total=int(total), offset=start, limit=size, **extra)


# ── Aggregations, groups, primary assets, changes ─────────────────────────


async def aggregate_inventory(
    db: AsyncSession,
    organization_id: uuid.UUID,
    *,
    entity: str = "assets",
    group_by: str = "category",
    query: str = "",
    **filters: Any,
) -> dict[str, Any]:
    """Counts of assets (or software) per value of one field, computed by PostgreSQL."""
    entity = (entity or "assets").strip().lower()
    field = (group_by or "").strip().lower()
    if entity == "software":
        if field not in SOFTWARE_GROUP_FIELDS:
            raise QueryError(f"software group_by is one of: {', '.join(SOFTWARE_GROUP_FIELDS)}")
        soft = OrganizationSoftware
        column = getattr(soft, field)
        where = [soft.organization_id == organization_id]
        if query.strip():
            where.append(soft.name_key.contains(_key(query)))
        rows = (
            await db.execute(
                select(column, func.count()).where(*where).group_by(column).order_by(func.count().desc(), column)
            )
        ).all()
        return _buckets(rows, entity, field)
    if entity != "assets":
        raise QueryError("entity is assets or software")
    if field not in ASSET_GROUP_FIELDS:
        raise QueryError(f"assets group_by is one of: {', '.join(ASSET_GROUP_FIELDS)}")
    where = store._asset_where(organization_id, q=str(query or ""), filters=_filters(**filters))
    if field == "source":
        rec = OrganizationAssetRecord
        rows = (
            await db.execute(
                select(rec.source, func.count(func.distinct(rec.asset_id)))
                .join(OrganizationAsset, OrganizationAsset.id == rec.asset_id)
                .where(*where)
                .group_by(rec.source)
                .order_by(func.count(func.distinct(rec.asset_id)).desc(), rec.source)
            )
        ).all()
    else:
        value = func.coalesce(store._field_value(field), "")
        rows = (
            await db.execute(
                select(value, func.count())
                .select_from(OrganizationAsset)
                .where(*where)
                .group_by(value)
                .order_by(func.count().desc(), value)
            )
        ).all()
    return _buckets(rows, entity, field)


def _buckets(rows: Sequence[Any], entity: str, field: str) -> dict[str, Any]:
    kept = [{"value": str(v) if v not in (None, "") else "(not set)", "count": int(c)} for v, c in rows]
    other = sum(int(str(b["count"])) for b in kept[MAX_BUCKETS:])
    return {
        "entity": entity,
        "group_by": field,
        "buckets": kept[:MAX_BUCKETS],
        "other": other,
        "total": sum(b["count"] for b in kept),
    }


async def list_asset_groups(
    db: AsyncSession,
    organization_id: uuid.UUID,
    *,
    query: str = "",
    offset: object = 0,
    limit: object = DEFAULT_PAGE,
) -> dict[str, Any]:
    """The asset groups with their member counts, by name, one SQL page."""
    grp, member = OrganizationAssetGroup, OrganizationAssetGroupMember
    size, start = _limit(limit), _offset(offset)
    where = [grp.organization_id == organization_id]
    if query.strip():
        where.append(grp.name_key.contains(_key(query)))
    total = (await db.execute(select(func.count()).select_from(grp).where(*where))).scalar() or 0
    counts = select(member.group_id, func.count().label("members")).group_by(member.group_id).subquery()
    rows = (
        await db.execute(
            select(grp.id, grp.name, grp.source, func.coalesce(counts.c.members, 0))
            .outerjoin(counts, counts.c.group_id == grp.id)
            .where(*where)
            .order_by(grp.name_key, grp.id)
            .offset(start)
            .limit(size)
        )
    ).all()
    items = [
        {"id": str(gid), "name": str(name), "source": str(source), "members": int(members)}
        for gid, name, source, members in rows
    ]
    return page(items, total=int(total), offset=start, limit=size)


async def list_primary_assets(
    db: AsyncSession,
    organization_id: uuid.UUID,
    *,
    query: str = "",
    offset: object = 0,
    limit: object = DEFAULT_PAGE,
) -> dict[str, Any]:
    """The primary assets the inventory supports, with their direct and group link counts, one SQL page."""
    link = OrganizationPrimaryAssetLink
    size, start = _limit(limit), _offset(offset)
    where = [link.organization_id == organization_id, link.removed.is_(False)]
    if query.strip():
        where.append(link.primary_asset_key.contains(_key(query)))
    total = (await db.execute(select(func.count(func.distinct(link.primary_asset_key))).where(*where))).scalar() or 0
    rows = (
        await db.execute(
            select(
                link.primary_asset_key,
                func.count(link.record_id),
                func.count(link.group_id),
            )
            .where(*where)
            .group_by(link.primary_asset_key)
            .order_by(link.primary_asset_key)
            .offset(start)
            .limit(size)
        )
    ).all()
    items = [
        {"primary_asset": str(key), "records": int(records), "groups": int(groups)} for key, records, groups in rows
    ]
    return page(items, total=int(total), offset=start, limit=size)


async def list_inventory_changes(
    db: AsyncSession,
    organization_id: uuid.UUID,
    *,
    since: str = "",
    before_id: object = None,
    limit: object = DEFAULT_PAGE,
) -> dict[str, Any]:
    """The inventory change log, newest first; page with ``before_id`` (the last id read)."""
    from app.services import organization_asset_records as records

    when: datetime | None = None
    if str(since or "").strip():
        try:
            when = datetime.fromisoformat(str(since).strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise QueryError("since is an ISO 8601 date or time, e.g. 2026-11-20 or 2026-11-20T08:00:00Z") from exc
    cursor: int | None = None
    if before_id not in (None, ""):
        try:
            cursor = int(str(before_id))
        except ValueError as exc:
            raise QueryError("before_id is the id of the last change read") from exc
    size = _limit(limit)
    rows = await records.changes(db, organization_id, since=when, before_id=cursor, limit=size)
    return {
        "items": rows,
        "limit": size,
        "next_before_id": rows[-1]["id"] if len(rows) == size else None,
    }


# ── Sizes, rendering ──────────────────────────────────────────────────────


async def inventory_sizes(db: AsyncSession, organization_id: uuid.UUID) -> dict[str, int]:
    """How many assets, software, asset groups and primary assets the inventory holds (four counts)."""
    link = OrganizationPrimaryAssetLink

    async def _count(stmt: Any) -> int:
        return int((await db.execute(stmt)).scalar() or 0)

    return {
        "assets": await _count(
            select(func.count())
            .select_from(OrganizationAsset)
            .where(OrganizationAsset.organization_id == organization_id)
        ),
        "software": await _count(
            select(func.count())
            .select_from(OrganizationSoftware)
            .where(OrganizationSoftware.organization_id == organization_id)
        ),
        "asset_groups": await _count(
            select(func.count())
            .select_from(OrganizationAssetGroup)
            .where(OrganizationAssetGroup.organization_id == organization_id)
        ),
        "primary_assets": await _count(
            select(func.count(func.distinct(link.primary_asset_key))).where(
                link.organization_id == organization_id, link.removed.is_(False)
            )
        ),
    }


def render(answer: Mapping[str, Any] | None, *, what: str) -> str:
    """An answer as an agent reads it: a one-line head, the data quarantined, the next call when paged."""
    from app.services.content_safety import wrap_untrusted

    if answer is None:
        return f"No {what} found. Search first (search_assets / search_software) and pass an id it answers."
    head = what
    if "total" in answer and "items" in answer:
        shown = len(answer["items"])
        start = int(answer.get("offset") or 0)
        head = (
            f"{what}: {answer['total']} matching, rows {start + 1}-{start + shown}" if shown else f"{what}: 0 matching"
        )
        nxt = answer.get("next_offset")
        head += f"; next page: offset={nxt}." if nxt is not None else "; end of the results."
    return "\n".join(
        [head, wrap_untrusted(json.dumps(answer, ensure_ascii=False, default=str), "organization inventory")]
    )
