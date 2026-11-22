# SPDX-License-Identifier: Apache-2.0

"""CTEM Cases: the versions a case's product runs on the organization's assets (#1252).

The running version of an upgrade case comes from the asset inventory (the
canonical asset store of #1178, ``organization_asset_software``), never from a
version typed in the tech stack. This module answers, for one case:

- the versions of the case's product seen across the assets, with the number
  of assets running each and the sources that report it;
- against the case's fixed version when it is known, how many assets are still
  below it and how many already run it or later (an ``unknown`` or unreadable
  version is counted apart, never guessed below or above); each asset is
  counted once in these totals, even when it runs several versions;
- when the fixed version is unknown, the sources that were checked for it.

:func:`running_versions` is pure (testable); :func:`read_running_versions`
feeds it from the store with one grouped read. Version order is the generic
scheme of ``ctem_fixed_version``.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable, Mapping
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.organization_asset import OrganizationAssetSoftware
from app.services import ctem_fixed_version as fixed_versions

logger = logging.getLogger(__name__)

#: The sources a fixed version is looked for in, in order, named when none answered.
FIXED_VERSION_SOURCES: tuple[str, ...] = (
    "OpenCTI Software that remediates the vulnerabilities",
    "course of action naming a version",
)
#: The most version rows a case answers; the rest are counted in ``more_versions``.
MAX_VERSIONS = 50
UNKNOWN = "unknown"


def _key(value: str | None) -> str:
    return " ".join(str(value or "").split()).casefold()


def product_keys(product_label: str | None) -> tuple[str, ...]:
    """The product keys a case label may stand for: the whole label, then without its vendor word.

    ``Microsoft Windows`` matches software stored as product ``Microsoft Windows``
    or as vendor ``Microsoft`` and product ``Windows``.
    """
    label = _key(product_label)
    if not label:
        return ()
    parts = label.split(" ", 1)
    return (label, parts[1]) if len(parts) == 2 else (label,)


def matches_case(product_label: str | None, vendor: str | None, product: str | None) -> bool:
    """Whether a software row (vendor, product) is the case's product."""
    label = _key(product_label)
    if not label:
        return False
    vendor_key, product_key = _key(vendor), _key(product)
    if product_key == label:
        return True
    return bool(vendor_key) and f"{vendor_key} {product_key}" == label


def running_versions(
    rows: Iterable[Mapping[str, Any]],
    *,
    product_label: str | None,
    fixed_version: str | None,
) -> dict[str, Any] | None:
    """The running versions of the case's product (pure); ``None`` when no asset runs it.

    *rows* hold one entry per (vendor, product, version) with ``vendor``,
    ``product``, ``version``, ``asset_ids`` and ``sources``. The answer lists
    versions with the most assets first, and with a fixed version known, counts
    the assets below it, at or above it, and those whose version cannot be
    compared. Each asset is counted once in those totals: an asset running a
    version below the fixed one is below, else one with a version that cannot
    be compared is not comparable, else it is on the fixed version, so the
    totals add up to ``asset_count``, the distinct assets running the product.
    """
    merged: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not matches_case(product_label, row.get("vendor"), row.get("product")):
            continue
        version = str(row.get("version") or "").strip() or UNKNOWN
        entry = merged.setdefault(version, {"version": version, "assets": set(), "sources": set()})
        entry["assets"].update(str(a) for a in row.get("asset_ids") or () if a)
        entry["sources"].update(str(s) for s in row.get("sources") or () if s)
    if not merged:
        return None
    fixed = (fixed_version or "").strip() or None
    # Per asset, the worst status of its versions: below_fixed, then not_comparable, then fixed.
    rank = {"below_fixed": 0, "not_comparable": 1, "fixed": 2}
    asset_status: dict[str, str] = {}
    versions: list[dict[str, Any]] = []
    for entry in merged.values():
        status = None
        if fixed:
            cmp = fixed_versions.compare_versions(entry["version"], fixed)
            status = "not_comparable" if cmp is None else "below_fixed" if cmp < 0 else "fixed"
            for asset in entry["assets"]:
                current = asset_status.get(asset)
                if current is None or rank[status] < rank[current]:
                    asset_status[asset] = status
        versions.append(
            {
                "version": entry["version"],
                "asset_count": len(entry["assets"]),
                "sources": sorted(entry["sources"]),
                **({"status": status} if status else {}),
            }
        )
    versions.sort(key=lambda v: (-v["asset_count"], fixed_versions.parse_version(v["version"]) is None, v["version"]))
    answer: dict[str, Any] = {
        "product_label": product_label,
        "fixed_version": fixed,
        "versions": versions[:MAX_VERSIONS],
        "more_versions": max(0, len(versions) - MAX_VERSIONS),
        "version_count": len(versions),
        "asset_count": len({a for e in merged.values() for a in e["assets"]}),
        "sources": sorted({s for v in versions for s in v["sources"]}),
    }
    if fixed:
        statuses = list(asset_status.values())
        answer["assets_below_fixed"] = statuses.count("below_fixed")
        answer["assets_on_fixed"] = statuses.count("fixed")
        answer["assets_not_comparable"] = statuses.count("not_comparable")
    else:
        answer["fixed_version_checked"] = list(FIXED_VERSION_SOURCES)
    return answer


async def for_case(db: AsyncSession, case: Any) -> dict[str, Any] | None:
    """The running versions of an upgrade *case* (its product, its fixed version), else ``None``."""
    if getattr(case, "remediation_kind", None) != "upgrade":
        return None
    return await read_running_versions(
        db,
        case.organization_id,
        product_label=case.product_label,
        fixed_version=case.fixed_version,
    )


async def read_running_versions(
    db: AsyncSession,
    organization_id: uuid.UUID,
    *,
    product_label: str | None,
    fixed_version: str | None,
) -> dict[str, Any] | None:
    """:func:`running_versions` over the organization's asset software (one grouped read).

    ``None`` when the case names no product, no asset runs it, or the store
    cannot be read (a case read never fails on the inventory).
    """
    keys = product_keys(product_label)
    if not keys:
        return None
    try:
        # A savepoint, so a failed read never leaves the caller's transaction aborted.
        async with db.begin_nested():
            counted, named = await _read_rows(db, organization_id, keys)
    except Exception:  # the inventory never breaks a case read
        logger.info("CTEM case running versions unavailable", exc_info=True)
        return None
    if not counted:
        return None
    rows = [
        {
            "vendor": vendor,
            "product": product,
            "version": version,
            "asset_ids": [str(a) for a in asset_ids or ()],
            "sources": sorted(named.get((vk, pk, version), ())),
        }
        for vk, pk, version, vendor, product, asset_ids in counted
    ]
    return running_versions(rows, product_label=product_label, fixed_version=fixed_version)


async def _read_rows(
    db: AsyncSession, organization_id: uuid.UUID, keys: tuple[str, ...]
) -> tuple[list[Any], dict[tuple[str, str, str], set[str]]]:
    """The (vendor, product, version) groups with their distinct asset ids, and the sources of each."""
    soft = OrganizationAssetSoftware
    where = (soft.organization_id == organization_id, soft.product_key.in_(keys))
    counted = list(
        (
            await db.execute(
                select(
                    soft.vendor_key,
                    soft.product_key,
                    soft.version,
                    func.min(soft.vendor),
                    func.min(soft.product),
                    func.array_agg(func.distinct(soft.asset_id)),
                )
                .where(*where)
                .group_by(soft.vendor_key, soft.product_key, soft.version)
            )
        ).all()
    )
    named: dict[tuple[str, str, str], set[str]] = {}
    if not counted:
        return counted, named
    source = func.jsonb_array_elements_text(soft.sources).label("source")
    for vk, pk, version, name in (
        await db.execute(select(soft.vendor_key, soft.product_key, soft.version, source).where(*where).distinct())
    ).all():
        named.setdefault((vk, pk, version), set()).add(str(name))
    return counted, named
