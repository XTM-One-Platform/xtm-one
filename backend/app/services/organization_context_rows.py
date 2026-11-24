# SPDX-License-Identifier: Apache-2.0

"""Row-level writes on the organization profile, for agents (#11888).

``update_organization_context`` replaces a whole field: an agent filling
the assets from an endpoint manager (Intune, an EDR, a CMDB) would re-send
every row and could overwrite the ones a person typed. This module is the
row path the ``organization_context`` group's ``upsert_organization_rows``
and ``remove_organization_rows`` tools stand on:

- assets and asset groups are keyed on ``source`` + ``external_id``; the
  source is the platform the rows come from (``intune``...), never a hand
  source (``manual``, ``csv``, ``api``): a row a person wrote is never
  updated or removed, and an incoming row named like one is refused;
- software products, shipped products (#1432) and AI systems have no
  source: a new name is added, an existing one is left as it is (never
  overwritten);
- every row is validated with the profile's own models (the taxonomy of the
  CSV and the import paths), a bad row is refused with its reason and the
  others go on;
- the plan is computed without I/O; the caller previews it or saves it as a
  new version. Since #1543 assets and asset groups are records
  (``organization_asset_records``): :func:`plan_record_upsert` and
  :func:`plan_record_remove` plan against the records the call addresses,
  and the record service writes only the rows that differ (no profile
  version, no full-profile copy, no cap).

The Intune mapping (:func:`intune_device_row`, :func:`intune_security_products`)
turns a managed device or a detected app into a profile row, so an agent
passes what Intune answered and the taxonomy stays the platform's.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ValidationError

from app.services.organization_context import (
    HAND_SOURCES,
    AssetEntry,
    AssetGroupEntry,
    OrganizationProfile,
    ShippedProduct,
    TechStackEntry,
)

#: section -> (profile field, row model, most rows the field holds).
ROW_SECTIONS: dict[str, tuple[str, type[BaseModel], int]] = {
    "assets": ("assets_exposure", AssetEntry, 5000),
    "asset_groups": ("asset_groups", AssetGroupEntry, 500),
    # #1495: the organization's software; ``tech_stack`` is its deprecated name, accepted for one release.
    "software": ("tech_stack", TechStackEntry, 500),
    "tech_stack": ("tech_stack", TechStackEntry, 500),
    # Deprecated alias since #1501 (one release): an AI system is an asset of category ai_target.
    "ai_systems": ("assets_exposure", AssetEntry, 5000),
    # #1432: what the organization ships, keyed on the product like the software.
    "shipped_products": ("shipped_products", ShippedProduct, 200),
}
#: Sections keyed on ``source`` + ``external_id`` (the others on the name).
SOURCED_SECTIONS: frozenset[str] = frozenset({"assets", "asset_groups"})
#: Sections where a row of another source with the same name is a second record of the same thing
#: (the canonical asset store merges them, #1178 S3): added beside it, never refused.
MULTI_SOURCE_SECTIONS: frozenset[str] = frozenset({"assets"})
#: Rows one call may carry.
MAX_BATCH = 500
#: Names listed per outcome in an answer.
LISTED = 25
#: Row formats the upsert maps on the server: ``profile`` rows are taken as they are.
ROW_FORMATS: tuple[str, ...] = ("profile", "intune_device", "intune_detected_app")
#: Fields a person may have set on a synced row, kept when the source does not send them.
_KEPT_FIELDS: tuple[str, ...] = ("environment", "owner", "tech_stack", "group", "description", "criticality")


@dataclass
class RowPlan:
    """What an upsert or a removal does to one section: the outcome per row and the profile it saves."""

    section: str
    source: str
    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    refused: list[tuple[str, str]] = field(default_factory=list)
    profile: OrganizationProfile | None = None
    #: Asset and group sections since #1543: the validated rows to write and the rows to remove,
    #: by ``(source, external_id)``, applied by the record service (never a profile version).
    writes: list[dict[str, Any]] = field(default_factory=list)
    remove_refs: list[tuple[str, str]] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.added or self.updated or self.removed)

    def as_dict(self) -> dict[str, Any]:
        return {
            "section": self.section,
            "source": self.source,
            "counts": {
                "added": len(self.added),
                "updated": len(self.updated),
                "unchanged": len(self.unchanged),
                "removed": len(self.removed),
                "refused": len(self.refused),
            },
            "added": self.added[:LISTED],
            "updated": self.updated[:LISTED],
            "removed": self.removed[:LISTED],
            "refused": [{"row": name, "reason": reason} for name, reason in self.refused[:LISTED]],
        }


class RowError(ValueError):
    """A call that cannot be planned at all (unknown section, hand source, too many rows)."""


def _row_label(raw: Any, index: int) -> str:
    if isinstance(raw, dict):
        for key in ("name", "product", "external_id", "deviceName", "displayName", "id"):
            if raw.get(key):
                return str(raw[key])[:200]
    return f"row {index + 1}"


def _first_error(exc: ValidationError) -> str:
    first = exc.errors()[0]
    where = ".".join(str(part) for part in first.get("loc", ())) or "row"
    return f"{where}: {first.get('msg', 'invalid value')}"


def check_call(section: str, source: str, count: int) -> None:
    """Refuse a call no row of which can be planned (``RowError``)."""
    if section not in ROW_SECTIONS:
        raise RowError(f"section must be one of {', '.join(ROW_SECTIONS)}")
    if section in SOURCED_SECTIONS:
        if not source.strip():
            raise RowError("name the source the rows come from (e.g. intune)")
        if source.strip().lower() in HAND_SOURCES:
            raise RowError(f"{source!r} is the source of rows a person wrote; name the platform the rows come from")
    if count > MAX_BATCH:
        raise RowError(f"at most {MAX_BATCH} rows per call; send the rest in another call")


def map_rows(rows: list[Any], row_format: str, *, include_owner: bool = False) -> list[Any]:
    """*rows* in the profile's shape: Intune objects mapped, anything else as it is."""
    if row_format not in ROW_FORMATS:
        raise RowError(f"rows_format must be one of {', '.join(ROW_FORMATS)}")
    if row_format == "intune_device":
        return [intune_device_row(r, include_owner=include_owner) if isinstance(r, dict) else r for r in rows]
    if row_format == "intune_detected_app":
        return intune_security_products([r for r in rows if isinstance(r, dict)])
    return list(rows)


def ai_system_row(raw: Any) -> Any:
    """A row sent to the deprecated ``ai_systems`` section as an ``ai_target`` asset row (#1501).

    A row in the old shape (``kind``, ``exposure``) is mapped as the profile upgrade maps it; an
    asset row keeps its fields and gets the category.
    """
    from app.services.organization_context import ai_system_asset

    if not isinstance(raw, dict):
        return raw
    if "kind" in raw or "exposure" in raw or "openaev_asset_id" in raw:
        mapped = ai_system_asset(raw)
        if mapped is None:
            return raw  # refused by the asset model with its own reason
        return {k: v for k, v in mapped.items() if v not in ("", None) or k in ("name", "category")}
    return {**raw, "category": "ai_target"}


def plan_upsert(profile: OrganizationProfile, section: str, source: str, rows: list[Any]) -> RowPlan:
    """What upserting *rows* from *source* into *section* does (no I/O, *profile* untouched)."""
    source = source.strip().lower()
    check_call(section, source, len(rows))
    attr, model, cap = ROW_SECTIONS[section]
    if section == "ai_systems":
        rows = [ai_system_row(r) for r in rows]
        if source:
            section = "assets"  # a platform's AI targets are its assets, keyed on its ids
    plan = RowPlan(section=section, source=source)
    draft = profile.model_copy(deep=True)
    current: list[Any] = list(getattr(draft, attr))
    sourced = section in SOURCED_SECTIONS
    name_key = "product" if section in ("software", "tech_stack", "shipped_products") else "name"
    by_key = {(r.source, r.external_id): i for i, r in enumerate(current) if sourced and r.external_id}
    by_name = {getattr(r, name_key).casefold(): r for r in current}
    seen: set[Any] = set()
    for index, raw in enumerate(rows):
        label = _row_label(raw, index)
        if not isinstance(raw, dict):
            plan.refused.append((label, "not an object"))
            continue
        data = dict(raw)
        if sourced:
            data["source"] = source
            if not str(data.get("external_id") or "").strip():
                plan.refused.append((label, "external_id is required: the id of the row in the source"))
                continue
        try:
            row = model.model_validate(data)
        except ValidationError as exc:
            plan.refused.append((label, _first_error(exc)))
            continue
        except ValueError as exc:
            plan.refused.append((label, str(exc)))
            continue
        name = getattr(row, name_key)
        key: Any = (source, getattr(row, "external_id", "")) if sourced else name.casefold()
        if key in seen:
            plan.refused.append((name, "sent twice in this call"))
            continue
        seen.add(key)
        if sourced and key in by_key:
            before = current[by_key[key]]
            # A field the source does not send keeps what is stored (a person may have set it).
            kept = {f: getattr(before, f) for f in _KEPT_FIELDS if hasattr(row, f) and f not in data}
            merged = row.model_copy(update=kept)
            if merged == before:
                plan.unchanged.append(name)
            else:
                current[by_key[key]] = merged
                plan.updated.append(name)
            continue
        other = by_name.get(name.casefold())
        if other is not None and section in MULTI_SOURCE_SECTIONS and getattr(other, "source", "") != source:
            other = None  # another source's record of the same thing: kept beside it, the store merges them
        if other is not None:
            if not sourced:
                plan.unchanged.append(name)
            else:
                owner = getattr(other, "source", "")
                why = "a person wrote it" if owner in HAND_SOURCES else f"it comes from {owner}"
                plan.refused.append((name, f"a row with this name exists and is kept ({why})"))
            continue
        if len(current) >= cap:
            plan.refused.append((name, f"the profile holds at most {cap} rows here"))
            continue
        current.append(row)
        by_name[name.casefold()] = row
        plan.added.append(name)
    setattr(draft, attr, current)
    plan.profile = draft
    return plan


def plan_remove(profile: OrganizationProfile, section: str, source: str, external_ids: list[str]) -> RowPlan:
    """What removing *source*'s rows *external_ids* from *section* does (assets and asset groups only)."""
    source = source.strip().lower()
    if section not in SOURCED_SECTIONS:
        raise RowError(
            "only assets and asset_groups rows can be removed by source; "
            "change the other sections with update_organization_context"
        )
    check_call(section, source, len(external_ids))
    attr = ROW_SECTIONS[section][0]
    plan = RowPlan(section=section, source=source)
    draft = profile.model_copy(deep=True)
    wanted = {str(i).strip() for i in external_ids if str(i).strip()}
    kept: list[Any] = []
    for row in getattr(draft, attr):
        if row.source == source and row.external_id in wanted:
            plan.removed.append(row.name)
            wanted.discard(row.external_id)
        else:
            kept.append(row)
    plan.refused.extend((ext, f"no {section} row of source {source} has this external_id") for ext in sorted(wanted))
    setattr(draft, attr, kept)
    plan.profile = draft
    return plan


def name_key(value: Any) -> str:
    """A row name as the record tables index it (``organization_asset_records._key``)."""
    return " ".join(str(value or "").split()).casefold()[:200]


#: Fields a stored record keeps when an agent's row does not send them (a person's edit, a sync's id, its software).
_RECORD_KEPT_FIELDS: tuple[str, ...] = (*_KEPT_FIELDS, "sync_id", "software")


def plan_record_upsert(
    section: str,
    source: str,
    rows: list[Any],
    *,
    validate: Callable[[dict[str, Any]], dict[str, Any]],
    stored: Mapping[tuple[str, str], Mapping[str, Any]],
    names: Mapping[str, str],
) -> RowPlan:
    """What upserting *rows* of *source* into the asset or group records does (no I/O, #1543).

    *stored* holds the records the rows address, by ``(source, external_id)``,
    as the record service answers them; *names* the name key of every other
    record a new row's name would clash with, mapped to its source (for
    assets, the rows of *source* only: another source's record of the same
    thing is merged by the store). *validate* is the record service's
    validation, so a row equal to its record is reported unchanged and
    nothing is written for it. No cap: the tables hold what the profile could not.
    """
    source = source.strip().lower()
    if section not in SOURCED_SECTIONS:
        raise RowError("only assets and asset_groups rows are records")
    check_call(section, source, len(rows))
    plan = RowPlan(section=section, source=source)
    taken = dict(names)
    seen: set[tuple[str, str]] = set()
    for index, raw in enumerate(rows):
        label = _row_label(raw, index)
        if not isinstance(raw, dict):
            plan.refused.append((label, "not an object"))
            continue
        external_id = str(raw.get("external_id") or "").strip()
        if not external_id:
            plan.refused.append((label, "external_id is required: the id of the row in the source"))
            continue
        ref = (source, external_id)
        before = stored.get(ref)
        data = {**raw, "source": source, "external_id": external_id}
        if before is not None:
            # A field the source does not send keeps what is stored (a person may have set it).
            data.update({f: before[f] for f in _RECORD_KEPT_FIELDS if f in before and f not in raw})
        try:
            value = validate(data)
        except ValueError as exc:
            plan.refused.append((label, str(exc)))
            continue
        name = str(value.get("name") or label)
        if ref in seen:
            plan.refused.append((name, "sent twice in this call"))
            continue
        seen.add(ref)
        if before is not None:
            if value == dict(before):
                plan.unchanged.append(name)
            else:
                plan.updated.append(name)
                plan.writes.append(value)
            continue
        owner = taken.get(name_key(name))
        if owner is not None:
            why = "a person wrote it" if owner in HAND_SOURCES else f"it comes from {owner}"
            plan.refused.append((name, f"a row with this name exists and is kept ({why})"))
            continue
        taken[name_key(name)] = source
        plan.added.append(name)
        plan.writes.append(value)
    return plan


def plan_record_remove(
    section: str, source: str, external_ids: list[str], *, stored: Mapping[tuple[str, str], Mapping[str, Any]]
) -> RowPlan:
    """What removing *source*'s records *external_ids* does (no I/O, #1543): *stored* as for an upsert."""
    source = source.strip().lower()
    if section not in SOURCED_SECTIONS:
        raise RowError(
            "only assets and asset_groups rows can be removed by source; "
            "change the other sections with update_organization_context"
        )
    check_call(section, source, len(external_ids))
    plan = RowPlan(section=section, source=source)
    for external_id in dict.fromkeys(str(i).strip() for i in external_ids if str(i).strip()):
        before = stored.get((source, external_id))
        if before is None:
            plan.refused.append((external_id, f"no {section} row of source {source} has this external_id"))
            continue
        plan.removed.append(str(before.get("name") or external_id))
        plan.remove_refs.append((source, external_id))
    return plan


def basis(plan: RowPlan, agent_name: str) -> str:
    """The provenance a saved row change keeps: what changed, from where, by which agent."""
    counts = f"{len(plan.added)} added, {len(plan.updated)} updated, {len(plan.removed)} removed"
    return f"rows of {plan.source or 'agent'} by agent {agent_name or 'unknown'}: {counts}"[:500]


# ── Intune mapping (no I/O) ───────────────────────────────────────────

#: Intune ``operatingSystem`` (lower-cased prefix) -> (category, subcategory) of a managed device.
_INTUNE_OS: tuple[tuple[str, tuple[str, str]], ...] = (
    ("ipados", ("mobile_device", "tablet")),
    ("ios", ("mobile_device", "smartphone")),
    ("android", ("mobile_device", "smartphone")),
    ("windows", ("host", "workstation")),
    ("macos", ("host", "workstation")),
    ("mac os", ("host", "workstation")),
    ("linux", ("host", "workstation")),
    ("chrome", ("host", "laptop")),
)
#: Detected app names (lower-cased substrings) that are security products, with their vendor.
SECURITY_PRODUCTS: tuple[tuple[str, str, str], ...] = (
    ("microsoft defender", "Microsoft Defender", "Microsoft"),
    ("crowdstrike", "CrowdStrike Falcon", "CrowdStrike"),
    ("sentinelone", "SentinelOne", "SentinelOne"),
    ("sophos", "Sophos Endpoint", "Sophos"),
    ("harfanglab", "HarfangLab", "HarfangLab"),
    ("cortex xdr", "Cortex XDR", "Palo Alto Networks"),
    ("globalprotect", "GlobalProtect", "Palo Alto Networks"),
    ("trend micro", "Trend Micro Apex One", "Trend Micro"),
    ("eset", "ESET Endpoint Security", "ESET"),
    ("bitdefender", "Bitdefender GravityZone", "Bitdefender"),
    ("tanium", "Tanium", "Tanium"),
    ("qualys", "Qualys Cloud Agent", "Qualys"),
    ("tenable", "Tenable Nessus Agent", "Tenable"),
    ("rapid7", "Rapid7 Insight Agent", "Rapid7"),
    ("zscaler", "Zscaler Client Connector", "Zscaler"),
    ("netskope", "Netskope Client", "Netskope"),
    ("1password", "1Password", "1Password"),
    ("bitlocker", "BitLocker", "Microsoft"),
)


def intune_device_row(device: dict[str, Any], *, include_owner: bool = False) -> dict[str, Any]:
    """An Intune managed device as an asset row (source ``intune``, external_id the device id).

    Category from the operating system (and ``chassisType`` laptop), the OS
    and the compliance state as tags; the primary user's display name is the
    owner only when *include_owner* (the administrator opted in).
    """
    os_name = str(device.get("operatingSystem") or "").strip()
    category, subcategory = "generic_asset", ""
    for prefix, taxonomy in _INTUNE_OS:
        if os_name.lower().startswith(prefix):
            category, subcategory = taxonomy
            break
    if category == "host" and str(device.get("chassisType") or "").lower() == "laptop":
        subcategory = "laptop"
    if category == "mobile_device" and "ipad" in str(device.get("model") or "").lower():
        subcategory = "tablet"
    tags = [f"os:{os_name.lower()}"] if os_name else []
    compliance = str(device.get("complianceState") or "").strip().lower()
    if compliance:
        tags.append(f"compliance:{compliance}")
    row: dict[str, Any] = {
        "name": str(device.get("deviceName") or device.get("id") or "").strip()[:200],
        "category": category,
        "subcategory": subcategory,
        "tags": tags,
        "external_id": str(device.get("id") or "").strip()[:200],
    }
    owner = str(device.get("userDisplayName") or "").strip()
    if include_owner and owner:
        row["owner"] = owner[:200]
    return row


def intune_security_products(apps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The security products among Intune detected apps, as software rows (one per product)."""
    found: dict[str, dict[str, Any]] = {}
    for app in apps:
        name = str(app.get("displayName") or app.get("name") or "").lower()
        for needle, product, vendor in SECURITY_PRODUCTS:
            if needle in name and product not in found:
                found[product] = {"product": product, "vendor": vendor}
    return list(found.values())
