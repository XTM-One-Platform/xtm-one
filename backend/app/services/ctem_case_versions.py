# SPDX-License-Identifier: Apache-2.0

"""CTEM Cases: the versions a case's product runs on the organization's assets (#1252).

The running version of an upgrade case comes from the asset inventory (the
canonical asset store of #1178, ``organization_asset_software``), never from a
version typed in the software inventory. An operating system case (``Microsoft Windows``)
also reads the assets' ``os_family`` and ``os_version`` fields, where Intune,
Defender and OpenAEV put the OS (#1430). This module answers, for one case:

- the versions of the case's product seen across the assets, with the number
  of assets running each and the sources that report it;
- against the case's fixed version when it is known, how many assets are still
  below it and how many already run it or later (an ``unknown`` or unreadable
  version is counted apart, never guessed below or above); each asset is
  counted once in these totals, even when it runs several versions;
- when the fixed version is unknown, the sources that were checked for it.

Rows are grouped by version and platform (the asset's ``os_family``, #1572):
many products ship a different fixed build per platform, so each asset is
compared with the fixed version of its own platform. A generic fixed version
(no platform named) over assets of several platforms is ``platform_unverified``:
an asset that is not below it reads ``not_comparable`` ("fix not known for this
platform") and the case never leaves scope on the inventory.

:func:`running_versions` is pure (testable); :func:`read_running_versions`
feeds it from the store with one grouped read. Version order is the generic
scheme of ``ctem_fixed_version``.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable, Mapping
from typing import Any

from sqlalchemy import ColumnElement, Select, func, or_, select
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
#: The most asset ids a version row carries; the rest are counted in ``more_asset_ids``.
MAX_ROW_ASSET_IDS = 20
UNKNOWN = "unknown"
#: Why an asset whose platform has no applicable fixed version is not compared (#1572).
FIX_NOT_KNOWN_FOR_PLATFORM = "fix not known for this platform"
#: Said on every answer: an asset running several versions is counted in each of their rows.
VERSION_ROWS_NOTE = "version rows may sum above asset_count: an asset running several versions is counted in each row"
_RANK = {"below_fixed": 0, "not_comparable": 1, "fixed": 2}


def _key(value: str | None) -> str:
    return " ".join(str(value or "").split()).casefold()


#: Company-form words a publisher name ends with in device inventories (``Google LLC``,
#: ``Microsoft Corporation``), dropped before a vendor is compared with a case label (#1736).
PUBLISHER_SUFFIXES: frozenset[str] = frozenset(
    {
        "inc",
        "inc.",
        "incorporated",
        "llc",
        "l.l.c.",
        "ltd",
        "ltd.",
        "limited",
        "corp",
        "corp.",
        "corporation",
        "co",
        "co.",
        "company",
        "gmbh",
        "ag",
        "sa",
        "s.a.",
        "sas",
        "bv",
        "b.v.",
        "nv",
        "plc",
        "oy",
        "ab",
        "as",
        "srl",
        "pty",
        "kk",
    }
)


def publisher_key(value: str | None) -> str:
    """A publisher folded without its company form and commas: ``Google, LLC`` reads ``google`` (#1736)."""
    words = _key(str(value or "").replace(",", " ")).split()
    while len(words) > 1 and words[-1] in PUBLISHER_SUFFIXES:
        words.pop()
    return " ".join(words)


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


#: Case product labels (folded) that name an operating system, and the ``os_family`` the
#: assets carry for it (``organization_context_microsoft.os_family`` names). Whole labels
#: only: ``Cisco IOS``, ``Google Chrome`` or ``Linux Kernel`` are not an asset's OS (#1430).
OS_FAMILY_LABELS: dict[str, str] = {
    "windows": "Windows",
    "microsoft windows": "Windows",
    "macos": "macOS",
    "apple macos": "macOS",
    "mac os x": "macOS",
    "apple mac os x": "macOS",
    "apple ios": "iOS",
    "apple iphone os": "iOS",
    "ipados": "iPadOS",
    "apple ipados": "iPadOS",
    "android": "Android",
    "google android": "Android",
    "chromeos": "ChromeOS",
    "chrome os": "ChromeOS",
    "google chromeos": "ChromeOS",
    "google chrome os": "ChromeOS",
}


def os_family_of(product_label: str | None) -> str | None:
    """The ``os_family`` an operating system case's product stands for, else ``None`` (#1430)."""
    return OS_FAMILY_LABELS.get(_key(product_label))


def _platform(value: Any) -> str | None:
    text = " ".join(str(value or "").split())
    return text or None


class FixResolver:
    """The fixed version that applies to an asset of one platform (pure, #1572).

    *by_platform* holds ``{platform: version}`` when the remediation names a
    platform; *generic* is the case's single fixed version. With a per-platform
    map, a platform it does not name has no applicable fix. With the generic fix
    alone over assets of several known platforms, the fix is ``unverified``: it
    still tells an asset below it, never one on it.
    """

    def __init__(
        self, generic: str | None, by_platform: Mapping[str, str] | None, platforms: Iterable[str | None]
    ) -> None:
        self.generic = (generic or "").strip() or None
        self.by_platform = {
            _key(p): str(v).strip() for p, v in (by_platform or {}).items() if _key(p) and str(v or "").strip()
        }
        known = {_key(p) for p in platforms if p}
        self.unverified = bool(self.generic) and not self.by_platform and len(known) > 1

    @property
    def known(self) -> bool:
        return bool(self.generic or self.by_platform)

    def status(self, version: str, platform: str | None) -> tuple[str, str | None]:
        """``(status, reason)`` of *version* on *platform*; reason only for a fix not known there."""
        if self.by_platform:
            fixed = self.by_platform.get(_key(platform))
            if fixed is None:
                return "not_comparable", FIX_NOT_KNOWN_FOR_PLATFORM
        else:
            fixed = self.generic
        cmp = fixed_versions.compare_versions(version, fixed or "")
        if cmp is None:
            return "not_comparable", None
        if cmp < 0:
            return "below_fixed", None
        if self.unverified:
            return "not_comparable", FIX_NOT_KNOWN_FOR_PLATFORM
        return "fixed", None


def os_rows(
    assets: Iterable[Mapping[str, Any]], *, product_label: str | None, skip: Iterable[str] = ()
) -> list[dict[str, Any]]:
    """Software-like rows of the case's OS from the assets' ``os_version`` (pure, #1430).

    *assets* hold ``asset_id``, ``name``, ``os_version`` and ``source`` (the source of the
    asset's OS field). An asset in *skip* (already counted by a software row of the
    product) is left out, so one asset never runs the OS twice.
    """
    skipped = {str(a) for a in skip}
    rows: list[dict[str, Any]] = []
    for asset in assets:
        asset_id = str(asset.get("asset_id") or "")
        if not asset_id or asset_id in skipped:
            continue
        source = str(asset.get("source") or "").strip()
        rows.append(
            {
                "name": asset.get("name"),
                "asset_id": asset_id,
                "asset_ids": [asset_id],
                "vendor": "",
                "product": product_label,
                "version": str(asset.get("os_version") or "").strip() or UNKNOWN,
                "platform": _platform(asset.get("os_family")) or os_family_of(product_label),
                "sources": [source] if source else [],
            }
        )
    return rows


def matches_case(product_label: str | None, vendor: str | None, product: str | None) -> bool:
    """Whether a software row (vendor, product) is the case's product.

    The publisher is compared without its company form (``Google LLC`` + ``Chrome``
    is ``Google Chrome``), and a product string that starts with its publisher
    (``Google LLC Google Chrome``) is read without it (#1736).
    """
    label = _key(product_label)
    if not label:
        return False
    vendor_key, product_key = publisher_key(vendor), _key(product)
    if product_key == label:
        return True
    if vendor_key and f"{vendor_key} {product_key}" == label:
        return True
    return _publisher_prefixed(label, vendor_key, product_key)


def _publisher_prefixed(label: str, vendor_key: str, product_key: str) -> bool:
    """Whether *product_key* is *label* behind a publisher name (``google llc google chrome``)."""
    if not product_key.endswith(" " + label):
        return False
    prefix = publisher_key(product_key[: -len(label) - 1])
    if not prefix:
        return False
    label_vendor = label.split(" ", 1)[0] if " " in label else ""
    return prefix in {vendor_key, label_vendor} - {""}


def _like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def product_filter(column: Any, keys: tuple[str, ...]) -> ColumnElement[bool]:
    """The product rows a case's *keys* stand for: an exact key, or the label behind a publisher.

    One bound pattern beside the key list, so the read stays bounded; :func:`matches_case`
    then keeps only the rows whose prefix is a publisher (#1736).
    """
    return or_(column.in_(keys), column.like("% " + _like_escape(keys[0]), escape="\\"))


def running_versions(
    rows: Iterable[Mapping[str, Any]],
    *,
    product_label: str | None,
    fixed_version: str | None,
    fixed_versions_by_platform: Mapping[str, str] | None = None,
    asset_platforms: Mapping[str, str | None] | None = None,
    with_devices: bool = False,
) -> dict[str, Any] | None:
    """The running versions of the case's product (pure); ``None`` when no asset runs it.

    *with_devices* adds ``devices``, ``{asset_id: {versions, status, sources}}`` for every
    asset (uncapped): the per-device state :mod:`ctem_case_version_history` compares
    between two recomputes (#1694); it is never served as is.

    *rows* hold one entry per (vendor, product, version) with ``vendor``,
    ``product``, ``version``, ``asset_ids``, ``sources`` and optionally
    ``platform``; *asset_platforms* names the platform (``os_family``) of an
    asset id when the row does not. The answer lists one row per (version,
    platform) with the most assets first and its asset ids (at most
    ``MAX_ROW_ASSET_IDS``); with a fixed version known it counts the assets
    below it, at or above it, and those that cannot be compared. Each asset is
    compared with the fixed version of its own platform (#1572) and counted
    once in those totals: an asset running a version below its fix is below,
    else one that cannot be compared is not comparable, else it is on the fixed
    version, so the totals add up to ``asset_count``, the distinct assets
    running the product.
    """
    platform_of = {str(k): _platform(v) for k, v in (asset_platforms or {}).items()}
    merged: dict[tuple[str, str | None], dict[str, Any]] = {}
    for row in rows:
        if not matches_case(product_label, row.get("vendor"), row.get("product")):
            continue
        version = str(row.get("version") or "").strip() or UNKNOWN
        for asset in (str(a) for a in row.get("asset_ids") or () if a):
            platform = _platform(row.get("platform")) or platform_of.get(asset)
            entry = merged.setdefault(
                (version, platform),
                {"version": version, "platform": platform, "assets": set(), "sources": set()},
            )
            entry["assets"].add(asset)
            entry["sources"].update(str(s) for s in row.get("sources") or () if s)
    if not merged:
        return None
    resolver = FixResolver(fixed_version, fixed_versions_by_platform, (e["platform"] for e in merged.values()))
    # Per asset, the worst status of its versions: below_fixed, then not_comparable, then fixed.
    asset_status: dict[str, str] = {}
    asset_reason: dict[str, str] = {}
    asset_versions: dict[str, set[str]] = {}
    asset_sources: dict[str, set[str]] = {}
    versions: list[dict[str, Any]] = []
    for entry in merged.values():
        status: str | None = None
        reason: str | None = None
        for asset in entry["assets"]:
            asset_versions.setdefault(asset, set()).add(entry["version"])
            asset_sources.setdefault(asset, set()).update(entry["sources"])
        if resolver.known:
            status, reason = resolver.status(entry["version"], entry["platform"])
            for asset in entry["assets"]:
                current = asset_status.get(asset)
                if current is None or _RANK[status] < _RANK[current]:
                    asset_status[asset] = status
                    if reason:
                        asset_reason[asset] = reason
                    else:
                        asset_reason.pop(asset, None)
        ids = sorted(entry["assets"])
        row_fix = resolver.by_platform.get(_key(entry["platform"])) if resolver.by_platform else None
        versions.append(
            {
                "version": entry["version"],
                "platform": entry["platform"],
                "asset_count": len(ids),
                "asset_ids": ids[:MAX_ROW_ASSET_IDS],
                "more_asset_ids": max(0, len(ids) - MAX_ROW_ASSET_IDS),
                "sources": sorted(entry["sources"]),
                **({"status": status} if status else {}),
                **({"status_reason": reason} if reason else {}),
                **({"fixed_version": row_fix} if row_fix else {}),
            }
        )
    versions.sort(
        key=lambda v: (
            -v["asset_count"],
            fixed_versions.parse_version(v["version"]) is None,
            v["version"],
            v["platform"] or "",
        )
    )
    answer: dict[str, Any] = {
        "product_label": product_label,
        "fixed_version": resolver.generic,
        "versions": versions[:MAX_VERSIONS],
        "more_versions": max(0, len(versions) - MAX_VERSIONS),
        "version_count": len({e["version"] for e in merged.values()}),
        "asset_count": len(asset_versions),
        "assets_multi_version": sum(1 for v in asset_versions.values() if len(v) > 1),
        "version_row_asset_sum": sum(v["asset_count"] for v in versions),
        "version_rows_note": VERSION_ROWS_NOTE,
        "platforms": sorted({e["platform"] for e in merged.values() if e["platform"]}),
        "sources": sorted({s for v in versions for s in v["sources"]}),
    }
    if resolver.by_platform:
        answer["fixed_versions"] = {
            str(p): str(v).strip()
            for p, v in sorted((fixed_versions_by_platform or {}).items())
            if _key(p) and str(v or "").strip()
        }
    if resolver.known:
        statuses = list(asset_status.values())
        answer["platform_unverified"] = resolver.unverified
        answer["assets_below_fixed"] = statuses.count("below_fixed")
        answer["assets_on_fixed"] = statuses.count("fixed")
        answer["assets_not_comparable"] = statuses.count("not_comparable")
        answer["assets_fix_not_known_for_platform"] = sum(
            1 for a, s in asset_status.items() if s == "not_comparable" and a in asset_reason
        )
    else:
        answer["fixed_version_checked"] = list(FIXED_VERSION_SOURCES)
    if with_devices:
        answer["devices"] = {
            asset: {
                "versions": sorted(asset_versions[asset]),
                "status": asset_status.get(asset),
                "sources": sorted(asset_sources.get(asset, ())),
            }
            for asset in sorted(asset_versions)
        }
    return answer


def all_on_fixed(answer: Mapping[str, Any] | None) -> bool:
    """Whether every asset running the product runs the fixed version or later (pure).

    ``False`` without a known fixed version, without any asset, or while one
    asset is below the fix or runs a version that cannot be compared: an
    unknown version is never read as fixed.
    """
    if not answer or not (answer.get("fixed_version") or answer.get("fixed_versions")):
        return False
    if answer.get("platform_unverified"):
        return False
    return (
        int(answer.get("assets_on_fixed") or 0) > 0
        and int(answer.get("assets_below_fixed") or 0) == 0
        and int(answer.get("assets_not_comparable") or 0) == 0
    )


def every_running_version(answer: Mapping[str, Any] | None) -> tuple[str, ...] | None:
    """Every version the inventory shows running, from a ``with_devices`` answer (pure, #2047).

    ``None`` when the answer carries no device (not read, or no asset runs the product). An asset
    whose version is not reported counts as :data:`UNKNOWN`, which compares with nothing, so an
    issue is never settled over an asset whose version nobody knows.
    """
    devices = (answer or {}).get("devices") or {}
    if not devices:
        return None
    versions: set[str] = set()
    for device in devices.values():
        versions.update(str(v) for v in device.get("versions") or () if str(v or "").strip())
        if not device.get("versions"):
            versions.add(UNKNOWN)
    return tuple(sorted(versions)) or None


async def fixed_in_inventory(db: AsyncSession, case: Any) -> bool:
    """Whether the inventory shows an upgrade *case* fixed on every asset (it leaves the case scope)."""
    if getattr(case, "remediation_kind", None) != "upgrade" or not getattr(case, "fixed_version", None):
        return False
    return all_on_fixed(await for_case(db, case))


async def for_case(db: AsyncSession, case: Any, *, with_devices: bool = False) -> dict[str, Any] | None:
    """The running versions of an upgrade *case* (its product, its fixed version), else ``None``.

    *with_devices*: see :func:`running_versions`; an inventory that answers but where no
    asset runs the product is then ``{"devices": {}}`` (``None`` stays "not read").
    """
    if getattr(case, "remediation_kind", None) != "upgrade":
        return None
    return await read_running_versions(
        db,
        case.organization_id,
        product_label=case.product_label,
        fixed_version=case.fixed_version,
        fixed_versions_by_platform=_case_platform_fixes(case),
        with_devices=with_devices,
    )


def _case_platform_fixes(case: Any) -> dict[str, str]:
    """The ``{platform: version}`` fixes a case keeps (#1572), ``{}`` when none."""
    raw = getattr(case, "fixed_versions", None)
    return {str(p): str(v) for p, v in raw.items() if p and v} if isinstance(raw, dict) else {}


async def read_running_versions(
    db: AsyncSession,
    organization_id: uuid.UUID,
    *,
    product_label: str | None,
    fixed_version: str | None,
    fixed_versions_by_platform: Mapping[str, str] | None = None,
    with_devices: bool = False,
) -> dict[str, Any] | None:
    """:func:`running_versions` over the organization's asset software (one grouped read).

    ``None`` when the case names no product, no asset runs it, or the store
    cannot be read (a case read never fails on the inventory).
    """
    keys = product_keys(product_label)
    if not keys:
        return None
    family = os_family_of(product_label)
    try:
        # A savepoint, so a failed read never leaves the caller's transaction aborted.
        async with db.begin_nested():
            counted, named = await _read_rows(db, organization_id, keys, family)
            os_assets = await _read_os_assets(db, organization_id, family) if family else []
            platforms = await _read_platforms(db, organization_id, keys) if counted else {}
    except Exception:  # the inventory never breaks a case read
        logger.info("CTEM case running versions unavailable", exc_info=True)
        return None
    if not counted and not os_assets:
        return {"devices": {}} if with_devices else None
    rows: list[dict[str, Any]] = [
        {
            "vendor": vendor,
            "product": product,
            "version": version,
            "asset_ids": [str(a) for a in asset_ids or ()],
            "sources": sorted(named.get((vk, pk, version), ())),
        }
        for vk, pk, version, vendor, product, asset_ids in counted
    ]
    counted_assets = {
        a for row in rows if matches_case(product_label, row["vendor"], row["product"]) for a in row["asset_ids"]
    }
    rows += os_rows(os_assets, product_label=product_label, skip=counted_assets)
    answer = running_versions(
        rows,
        product_label=product_label,
        fixed_version=fixed_version,
        fixed_versions_by_platform=fixed_versions_by_platform,
        asset_platforms=platforms,
        with_devices=with_devices,
    )
    if answer is None and with_devices:
        return {"devices": {}}
    return answer


def inventory_version_counts(
    rows: Iterable[Any], os_assets: Mapping[str, list[dict[str, Any]]], labels: Iterable[str]
) -> dict[str, dict[str, int]]:
    """Per product label, each known version the device inventory reports and its distinct assets (pure, #2038).

    *rows* are ``(vendor, product, version, asset_ids)`` groups of the asset software; *os_assets*
    the assets of each ``os_family`` (:func:`_read_os_assets`). A row counts for a label by the
    rule :func:`running_versions` reads (:func:`matches_case`), and an OS label also counts the
    assets' ``os_version`` (an asset already counted by a software row is not counted twice). An
    ``unknown`` version is left out: it names nothing OpenCTI could place. A label no asset runs
    is absent.
    """
    rows = list(rows)
    out: dict[str, dict[str, int]] = {}
    for label in dict.fromkeys(labels):
        on_version: dict[str, set[str]] = {}
        counted: set[str] = set()
        for vendor, product, version, asset_ids in rows:
            if not matches_case(label, vendor, product):
                continue
            ids = {str(a) for a in asset_ids or ()}
            counted |= ids
            text = str(version or "").strip()
            if text and text != UNKNOWN:
                on_version.setdefault(text, set()).update(ids)
        family = os_family_of(label)
        for row in os_rows(os_assets.get(family) or [], product_label=label, skip=counted) if family else []:
            if row["version"] != UNKNOWN:
                on_version.setdefault(row["version"], set()).update(row["asset_ids"])
        if on_version:
            out[label] = {v: len(ids) for v, ids in on_version.items()}
    return out


#: Product labels per grouped inventory read of :func:`inventory_versions` (bounded bind parameters).
_INVENTORY_LABELS_CHUNK = 100


async def inventory_versions(
    db: AsyncSession, organization_id: uuid.UUID, labels: Iterable[str]
) -> dict[str, dict[str, int]]:
    """Per product label, the known versions the device inventory reports with their asset counts (#2038).

    The same rows :func:`read_running_versions` reads for a case, so the CVE matcher checks every
    version a case shows as running. ``{}`` when the store cannot be read (the matcher then keeps
    the versions of the software entities and the profile).
    """
    wanted = [label for label in dict.fromkeys(labels) if product_keys(label)]
    if not wanted:
        return {}
    soft = OrganizationAssetSoftware
    rows: list[Any] = []
    os_assets: dict[str, list[dict[str, Any]]] = {}
    try:
        async with db.begin_nested():
            for start in range(0, len(wanted), _INVENTORY_LABELS_CHUNK):
                chunk = wanted[start : start + _INVENTORY_LABELS_CHUNK]
                keys = sorted({k for label in chunk for k in product_keys(label)})
                like = [
                    soft.product_key.like("% " + _like_escape(product_keys(label)[0]), escape="\\") for label in chunk
                ]
                rows.extend(
                    (
                        await db.execute(
                            select(
                                func.min(soft.vendor),
                                func.min(soft.product),
                                soft.version,
                                func.array_agg(func.distinct(soft.asset_id)),
                            )
                            .where(soft.organization_id == organization_id, or_(soft.product_key.in_(keys), *like))
                            .group_by(soft.vendor_key, soft.product_key, soft.version)
                        )
                    ).all()
                )
            for family in sorted({f for f in (os_family_of(label) for label in wanted) if f}):
                os_assets[family] = await _read_os_assets(db, organization_id, family)
    except Exception:  # the inventory never breaks the issues read
        logger.info("CTEM inventory versions unavailable", exc_info=True)
        return {}
    return inventory_version_counts(rows, os_assets, wanted)


async def inventory_sources(db: AsyncSession, organization_id: uuid.UUID) -> list[str] | None:
    """The sources the organization's asset software was imported from, sorted (#1736).

    Named when a case's product runs on no asset, so an ask never asks a person
    what an imported source already answered. ``None`` when the store cannot be read.
    """
    soft = OrganizationAssetSoftware
    source = func.jsonb_array_elements_text(soft.sources).label("source")
    try:
        async with db.begin_nested():
            result = await db.execute(select(source).where(soft.organization_id == organization_id).distinct())
    except Exception:  # the inventory never breaks a case read
        logger.info("CTEM inventory sources unavailable", exc_info=True)
        return None
    return sorted({str(name) for (name,) in result.all() if name})


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
    rows: Iterable[Mapping[str, Any]],
    *,
    product_label: str | None,
    fixed_version: str | None,
    fixed_versions_by_platform: Mapping[str, str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Per asset name key, the versions of the case's product it runs (pure).

    *rows* hold ``name``, ``asset_id``, ``vendor``, ``product``, ``version``,
    ``sources`` and optionally ``platform`` (one software entry of one asset).
    With a fixed version known, each asset gets the worst status of its
    versions against the fix of its platform, as :func:`running_versions`
    counts it (``status_reason`` when the fix is not known for its platform).
    """
    kept = [r for r in rows if matches_case(product_label, r.get("vendor"), r.get("product"))]
    resolver = FixResolver(fixed_version, fixed_versions_by_platform, (_platform(r.get("platform")) for r in kept))
    out: dict[str, dict[str, Any]] = {}
    for row in kept:
        version = str(row.get("version") or "").strip() or UNKNOWN
        platform = _platform(row.get("platform"))
        entry = out.setdefault(
            _key(row.get("name")), {"asset_id": str(row.get("asset_id") or ""), "versions": [], "sources": []}
        )
        if platform and not entry.get("platform"):
            entry["platform"] = platform
        if version not in entry["versions"]:
            entry["versions"].append(version)
        for source in row.get("sources") or ():
            if source and str(source) not in entry["sources"]:
                entry["sources"].append(str(source))
        if resolver.known:
            status, reason = resolver.status(version, platform)
            current = entry.get("status")
            if current is None or _RANK[status] < _RANK[current]:
                entry["status"] = status
                if reason:
                    entry["status_reason"] = reason
                else:
                    entry.pop("status_reason", None)
    for entry in out.values():
        entry["versions"].sort(key=lambda v: (fixed_versions.parse_version(v) is None, v))
        entry["sources"].sort()
    return out


#: How an issue's asset list was filled when its source names none (#2047).
ASSETS_FROM_INVENTORY = "inventory"


def attribute_assets(issues: list[dict[str, Any]], names: Iterable[str]) -> int:
    """Give each visible issue that names no asset the assets running the case's product (pure, #2047).

    A CVE read from OpenCTI on a product names no host: on an upgrade case every asset running the
    product is exposed to it, so its list is the inventory's. An issue with assets of its own keeps
    them; a restricted row is left as is. Answers how many issues were filled.
    """
    running = list(dict.fromkeys(str(n).strip() for n in names if str(n or "").strip()))
    if not running:
        return 0
    filled = 0
    for issue in issues:
        if issue.get("restricted") or issue.get("assets"):
            continue
        issue["assets"] = list(running)
        issue["assets_from"] = ASSETS_FROM_INVENTORY
        filled += 1
    return filled


async def running_asset_names(db: AsyncSession, case: Any) -> list[str]:
    """The names of the assets running an upgrade *case*'s product (software or OS rows), ``[]`` when none."""
    if getattr(case, "remediation_kind", None) != "upgrade":
        return []
    keys = product_keys(getattr(case, "product_label", None))
    if not keys:
        return []
    family = os_family_of(getattr(case, "product_label", None))
    try:
        async with db.begin_nested():
            rows = await _read_running_names(db, case.organization_id, keys)
            os_assets = await _read_os_assets(db, case.organization_id, family) if family else []
    except Exception:  # the inventory never breaks a case read
        logger.info("CTEM case running assets unavailable", exc_info=True)
        return []
    names = [name for name, vendor, product in rows if matches_case(case.product_label, vendor, product)]
    names += [str(a.get("name") or "") for a in os_assets]
    return sorted(dict.fromkeys(n for n in names if n), key=_key)


async def assets_for_case(db: AsyncSession, case: Any, issues: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """The affected assets of a case as its reader sees them, with the version each runs (#1176 slice 3b).

    The assets come from the visible issues only; an upgrade case adds, per
    asset, the versions of its product the inventory reports (``versions``,
    ``sources``, ``status`` against the fixed version) and ``version_source``
    ``inventory``; any other case, or an inventory that cannot be read, answers
    ``version_source`` ``none``, never a version typed in the software inventory.
    """
    answer = affected_assets(issues)
    answer["version_source"] = "none"
    if getattr(case, "remediation_kind", None) != "upgrade" or not answer["assets"]:
        return answer
    keys = product_keys(getattr(case, "product_label", None))
    if not keys:
        return answer
    names = [_key(a["name"]) for a in answer["assets"]]
    family = os_family_of(getattr(case, "product_label", None))
    try:
        async with db.begin_nested():
            found = await _read_asset_rows(db, case.organization_id, keys, names, family)
            os_assets = await _read_os_assets(db, case.organization_id, family, names) if family else []
    except Exception:  # the inventory never breaks a case read
        logger.info("CTEM case asset versions unavailable", exc_info=True)
        return answer
    counted_assets = {
        str(r["asset_id"]) for r in found if matches_case(case.product_label, r.get("vendor"), r.get("product"))
    }
    found = [*found, *os_rows(os_assets, product_label=case.product_label, skip=counted_assets)]
    by_asset = versions_on_assets(
        found,
        product_label=case.product_label,
        fixed_version=case.fixed_version,
        fixed_versions_by_platform=_case_platform_fixes(case),
    )
    answer["platform_unverified"] = any(s.get("status_reason") == FIX_NOT_KNOWN_FOR_PLATFORM for s in by_asset.values())
    answer["version_source"] = "inventory"
    for row in answer["assets"]:
        seen = by_asset.get(_key(row["name"]))
        row["in_inventory"] = seen is not None
        if seen:
            row.update(seen)
    return answer


def os_case_rows(soft: Any, asset: Any, family: str | None) -> list[ColumnElement[bool]]:
    """The conditions an operating system case puts on software rows (#2036), ``[]`` for any other case.

    A package (a row with a purl: the Rust ``windows`` crate of a repository) is
    never the OS, and an asset whose known ``os_family`` is another OS (a macOS
    laptop) never runs it; an asset with no OS family known keeps its row.
    """
    if not family:
        return []
    os_family = asset.fields["os_family"]["value"].astext
    return [soft.purl == "", or_(os_family.is_(None), os_family == "", os_family == family)]


async def _read_asset_rows(
    db: AsyncSession, organization_id: uuid.UUID, keys: tuple[str, ...], names: list[str], family: str | None = None
) -> list[dict[str, Any]]:
    """The software entries of the case's product on the named assets (one read)."""
    soft, asset = OrganizationAssetSoftware, OrganizationAsset
    result = await db.execute(
        select(
            asset.name_key,
            asset.id,
            soft.vendor,
            soft.product,
            soft.version,
            soft.sources,
            asset.fields["os_family"]["value"].astext,
        )
        .join(asset, asset.id == soft.asset_id)
        .where(
            soft.organization_id == organization_id,
            asset.organization_id == organization_id,
            asset.name_key.in_([n[:300] for n in names]),
            product_filter(soft.product_key, keys),
            *os_case_rows(soft, asset, family),
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
            "platform": _platform(platform),
        }
        for name, asset_id, vendor, product, version, sources, platform in result.all()
    ]


async def _read_running_names(
    db: AsyncSession, organization_id: uuid.UUID, keys: tuple[str, ...]
) -> list[tuple[str, str, str]]:
    """(asset name, vendor, product) of every asset with a software row of *keys* (one read, #2047)."""
    soft, asset = OrganizationAssetSoftware, OrganizationAsset
    result = await db.execute(
        select(asset.name, soft.vendor, soft.product)
        .join(asset, asset.id == soft.asset_id)
        .where(
            soft.organization_id == organization_id,
            asset.organization_id == organization_id,
            product_filter(soft.product_key, keys),
        )
        .distinct()
    )
    return [(str(n or ""), str(v or ""), str(p or "")) for n, v, p in result.all()]


def _platforms_query(organization_id: uuid.UUID, keys: tuple[str, ...]) -> Select[Any]:
    """The platform (``os_family``) of every asset running one of *keys* (#1572).

    A join on the software rows, never a list of asset ids: the bind parameters
    are the organization and the product keys, so a product installed on more
    assets than the driver's parameter cap still reads.
    """
    asset = OrganizationAsset
    soft = OrganizationAssetSoftware
    return (
        select(asset.id, asset.fields["os_family"]["value"].astext)
        .join(soft, soft.asset_id == asset.id)
        .where(
            asset.organization_id == organization_id,
            soft.organization_id == organization_id,
            product_filter(soft.product_key, keys),
        )
        .distinct()
    )


async def _read_platforms(db: AsyncSession, organization_id: uuid.UUID, keys: tuple[str, ...]) -> dict[str, str | None]:
    """The platform (``os_family``) of each asset running the case's product, one read (#1572)."""
    if not keys:
        return {}
    result = await db.execute(_platforms_query(organization_id, keys))
    return {str(asset_id): _platform(platform) for asset_id, platform in result.all()}


async def _read_os_assets(
    db: AsyncSession, organization_id: uuid.UUID, family: str, names: list[str] | None = None
) -> list[dict[str, Any]]:
    """The assets whose ``os_family`` is *family*, with their ``os_version`` and its source (one read)."""
    asset = OrganizationAsset
    fields = asset.fields
    where = [asset.organization_id == organization_id, fields["os_family"]["value"].astext == family]
    if names is not None:
        where.append(asset.name_key.in_([n[:300] for n in names]))
    result = await db.execute(
        select(
            asset.id,
            asset.name_key,
            fields["os_version"]["value"].astext,
            func.coalesce(fields["os_version"]["source"].astext, fields["os_family"]["source"].astext),
        ).where(*where)
    )
    return [
        {"asset_id": asset_id, "name": name, "os_version": version, "source": source}
        for asset_id, name, version, source in result.all()
    ]


async def _read_rows(
    db: AsyncSession, organization_id: uuid.UUID, keys: tuple[str, ...], family: str | None = None
) -> tuple[list[Any], dict[tuple[str, str, str], set[str]]]:
    """The (vendor, product, version) groups with their distinct asset ids, and the sources of each.

    An operating system case (*family*) keeps only the rows :func:`os_case_rows` allows (#2036).
    """
    soft, asset = OrganizationAssetSoftware, OrganizationAsset
    where = (
        soft.organization_id == organization_id,
        product_filter(soft.product_key, keys),
        *os_case_rows(soft, asset, family),
    )
    joined = family is not None

    def base(statement: Select[Any]) -> Select[Any]:
        return statement.join(asset, asset.id == soft.asset_id) if joined else statement

    counted = list(
        (
            await db.execute(
                base(
                    select(
                        soft.vendor_key,
                        soft.product_key,
                        soft.version,
                        func.min(soft.vendor),
                        func.min(soft.product),
                        func.array_agg(func.distinct(soft.asset_id)),
                    )
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
        await db.execute(base(select(soft.vendor_key, soft.product_key, soft.version, source)).where(*where).distinct())
    ).all():
        named.setdefault((vk, pk, version), set()).add(str(name))
    return counted, named
