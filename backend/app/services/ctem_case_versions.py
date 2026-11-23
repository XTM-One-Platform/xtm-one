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

from app.models.organization_asset import OrganizationAsset, OrganizationAssetSoftware
from app.services import ctem_fixed_version as fixed_versions

logger = logging.getLogger(__name__)

#: The sources a fixed version is looked for in, in order, named when none answered.
FIXED_VERSION_SOURCES: tuple[str, ...] = (
    "OpenCTI Software that remediates the vulnerabilities",
    "course of action naming a version",
)
#: The most version rows a case answers; the rest are counted in ``more_versions``.
MAX_VERSIONS = 50
#: The most affected asset rows a case answers; the rest are counted in ``more_assets``.
MAX_ASSETS = 500
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


def all_on_fixed(answer: Mapping[str, Any] | None) -> bool:
    """Whether every asset running the product runs the fixed version or later (pure).

    ``False`` without a known fixed version, without any asset, or while one
    asset is below the fix or runs a version that cannot be compared: an
    unknown version is never read as fixed.
    """
    if not answer or not answer.get("fixed_version"):
        return False
    return (
        int(answer.get("assets_on_fixed") or 0) > 0
        and int(answer.get("assets_below_fixed") or 0) == 0
        and int(answer.get("assets_not_comparable") or 0) == 0
    )


async def fixed_in_inventory(db: AsyncSession, case: Any) -> bool:
    """Whether the inventory shows an upgrade *case* fixed on every asset (it leaves the case scope)."""
    if getattr(case, "remediation_kind", None) != "upgrade" or not getattr(case, "fixed_version", None):
        return False
    return all_on_fixed(await for_case(db, case))


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


def affected_assets(issues: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """The assets the visible issues of a case name, one row per asset (pure).

    *issues* are the rendered issues of the case read (restricted rows carry no
    asset and are skipped). Each row counts the issues naming the asset, the open
    ones, whether one of them is known exploited and the highest CVSS; the rows
    most exposed come first (open issues, then KEV, then CVSS, then the name).
    Names fold case and spaces, the first spelling met is shown. KEV and CVSS
    are the facts of the issue's CVE folded across every visible source: an
    asset named by an OpenAEV finding of a CVE that OpenCTI marks known
    exploited reads KEV yes (UX r1 B3 on #1324).
    """
    visible = [issue for issue in issues if not issue.get("restricted")]
    cve_kev: dict[str, bool] = {}
    cve_cvss: dict[str, float] = {}
    for issue in visible:
        cve = str(issue.get("cve") or "").strip().upper()
        if not cve:
            continue
        cve_kev[cve] = cve_kev.get(cve, False) or bool(issue.get("kev"))
        cvss = issue.get("cvss")
        if isinstance(cvss, int | float) and cvss > cve_cvss.get(cve, -1.0):
            cve_cvss[cve] = float(cvss)
    rows: dict[str, dict[str, Any]] = {}
    for issue in visible:
        cve = str(issue.get("cve") or "").strip().upper()
        kev = bool(issue.get("kev")) or cve_kev.get(cve, False)
        cvss = cve_cvss.get(cve, issue.get("cvss")) if cve else issue.get("cvss")
        for name in dict.fromkeys(str(a).strip() for a in issue.get("assets") or () if str(a).strip()):
            row = rows.setdefault(
                _key(name),
                {"name": name, "issue_count": 0, "open_issue_count": 0, "kev": False, "max_cvss": None},
            )
            row["issue_count"] += 1
            row["open_issue_count"] += 1 if issue.get("open") else 0
            row["kev"] = row["kev"] or kev
            if isinstance(cvss, int | float) and (row["max_cvss"] is None or cvss > row["max_cvss"]):
                row["max_cvss"] = float(cvss)
    ordered = sorted(
        rows.values(),
        key=lambda r: (-r["open_issue_count"], not r["kev"], -(r["max_cvss"] or 0.0), _key(r["name"])),
    )
    return {
        "assets": ordered[:MAX_ASSETS],
        "asset_count": len(ordered),
        "more_assets": max(0, len(ordered) - MAX_ASSETS),
    }


def versions_on_assets(
    rows: Iterable[Mapping[str, Any]], *, product_label: str | None, fixed_version: str | None
) -> dict[str, dict[str, Any]]:
    """Per asset name key, the versions of the case's product it runs (pure).

    *rows* hold ``name``, ``asset_id``, ``vendor``, ``product``, ``version`` and
    ``sources`` (one software entry of one asset). With a fixed version known,
    each asset gets the worst status of its versions, as :func:`running_versions`
    counts it.
    """
    rank = {"below_fixed": 0, "not_comparable": 1, "fixed": 2}
    fixed = (fixed_version or "").strip() or None
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not matches_case(product_label, row.get("vendor"), row.get("product")):
            continue
        version = str(row.get("version") or "").strip() or UNKNOWN
        entry = out.setdefault(
            _key(row.get("name")), {"asset_id": str(row.get("asset_id") or ""), "versions": [], "sources": []}
        )
        if version not in entry["versions"]:
            entry["versions"].append(version)
        for source in row.get("sources") or ():
            if source and str(source) not in entry["sources"]:
                entry["sources"].append(str(source))
        if fixed:
            cmp = fixed_versions.compare_versions(version, fixed)
            status = "not_comparable" if cmp is None else "below_fixed" if cmp < 0 else "fixed"
            current = entry.get("status")
            if current is None or rank[status] < rank[current]:
                entry["status"] = status
    for entry in out.values():
        entry["versions"].sort(key=lambda v: (fixed_versions.parse_version(v) is None, v))
        entry["sources"].sort()
    return out


async def assets_for_case(db: AsyncSession, case: Any, issues: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """The affected assets of a case as its reader sees them, with the version each runs (#1176 slice 3b).

    The assets come from the visible issues only; an upgrade case adds, per
    asset, the versions of its product the inventory reports (``versions``,
    ``sources``, ``status`` against the fixed version) and ``version_source``
    ``inventory``; any other case, or an inventory that cannot be read, answers
    ``version_source`` ``none``, never a version typed in the tech stack.
    """
    answer = affected_assets(issues)
    answer["version_source"] = "none"
    if getattr(case, "remediation_kind", None) != "upgrade" or not answer["assets"]:
        return answer
    keys = product_keys(getattr(case, "product_label", None))
    if not keys:
        return answer
    names = [_key(a["name"]) for a in answer["assets"]]
    try:
        async with db.begin_nested():
            found = await _read_asset_rows(db, case.organization_id, keys, names)
    except Exception:  # the inventory never breaks a case read
        logger.info("CTEM case asset versions unavailable", exc_info=True)
        return answer
    by_asset = versions_on_assets(found, product_label=case.product_label, fixed_version=case.fixed_version)
    answer["version_source"] = "inventory"
    for row in answer["assets"]:
        seen = by_asset.get(_key(row["name"]))
        row["in_inventory"] = seen is not None
        if seen:
            row.update(seen)
    return answer


async def _read_asset_rows(
    db: AsyncSession, organization_id: uuid.UUID, keys: tuple[str, ...], names: list[str]
) -> list[dict[str, Any]]:
    """The software entries of the case's product on the named assets (one read)."""
    soft, asset = OrganizationAssetSoftware, OrganizationAsset
    result = await db.execute(
        select(asset.name_key, asset.id, soft.vendor, soft.product, soft.version, soft.sources)
        .join(asset, asset.id == soft.asset_id)
        .where(
            soft.organization_id == organization_id,
            soft.product_key.in_(keys),
            asset.organization_id == organization_id,
            asset.name_key.in_([n[:300] for n in names]),
        )
    )
    return [
        {
            "name": name,
            "asset_id": asset_id,
            "vendor": vendor,
            "product": product,
            "version": version,
            "sources": sources,
        }
        for name, asset_id, vendor, product, version, sources in result.all()
    ]


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
