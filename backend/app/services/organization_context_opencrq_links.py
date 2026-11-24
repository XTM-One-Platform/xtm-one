# SPDX-License-Identifier: Apache-2.0

"""Supporting asset -> primary asset links from OpenCRQ (#1591, option ``link_supporting_assets``).

OpenCRQ rates its PRIMARY assets (the business assets the profile lists as
primary assets, named crown jewels before #1591) and links each to the
SUPPORTING assets (the IT) underneath with ``support_to_primary`` links,
each carrying one or more roles (``stores``, ``processes``...). The roles say
which security needs the supporting asset inherits
(``module/src/modules/asset-dependencies/link-roles.ts``, mirrored in
:data:`LINK_ROLE_INHERITANCE`).

The OpenCRQ primary-asset sync, with its option ``link_supporting_assets``
(on unless the sync's filters say ``"false"``), writes those links on
``organization_crown_jewel_links`` with ``source="opencrq"``: the supporting
asset is the inventory record of the same OpenCRQ id (a record of source
``opencrq``), else the one ASSET of the same name, hostname or FQDN (a device
Intune and OpenAEV both report is one asset with two records: one match, its
preferred record linked), else the one asset of the same loose name
(apostrophes dropped, hyphen, underscore, dot and space one separator:
``Jamies-MacBook-Air`` is ``Jamie's MacBook Air``). Rules:

- a manual link (``source="manual"``) is never touched by the sync;
- a synced link a person removed stays as a tombstone (``removed``): the sync
  never adds it back (a manual link or removal wins);
- a synced link OpenCRQ no longer holds is deleted at the next sync;
- a supporting asset no record names is reported, never created;
- a name that reaches two or more distinct assets is reported ambiguous, never guessed.

:func:`plan_links` is the rule (no I/O); :func:`sync_links` reads OpenCRQ and
writes. ``depends_on`` chains (support_to_support) are resolved by the
signals-engine slice; this module writes direct links only.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.organization_asset_record import OrganizationAssetRecord, OrganizationCrownJewelLink

#: The sync filter that turns the link import off ("false"); absent or anything else: on.
LINK_OPTION = "link_supporting_assets"
SECURITY_NEEDS: tuple[str, ...] = ("availability", "integrity", "confidentiality", "traceability")
_INHERITANCE_ORDER: tuple[str, ...] = ("none", "lower", "full")
#: OpenCRQ ``LINK_ROLE_INHERITANCE``: role -> need -> none / lower / full.
LINK_ROLE_INHERITANCE: dict[str, dict[str, str]] = {
    "stores": {"availability": "full", "integrity": "full", "confidentiality": "full", "traceability": "lower"},
    "processes": {"availability": "full", "integrity": "full", "confidentiality": "full", "traceability": "lower"},
    "reads": {"availability": "lower", "integrity": "lower", "confidentiality": "full", "traceability": "none"},
    "transmits": {"availability": "full", "integrity": "full", "confidentiality": "full", "traceability": "none"},
    "administers": {"availability": "full", "integrity": "full", "confidentiality": "full", "traceability": "full"},
    "monitors": {"availability": "none", "integrity": "none", "confidentiality": "lower", "traceability": "full"},
    "supports_availability": {
        "availability": "full",
        "integrity": "none",
        "confidentiality": "none",
        "traceability": "none",
    },
    "performs": {"availability": "full", "integrity": "full", "confidentiality": "lower", "traceability": "lower"},
    "unknown": {"availability": "full", "integrity": "full", "confidentiality": "full", "traceability": "full"},
}
#: Links read at most per sync.
MAX_LINKS = 5000


def link_option_on(filters: dict[str, Any] | None) -> bool:
    """Whether a sync imports the links: on by default, off only when its filter says so."""
    value = (filters or {}).get(LINK_OPTION)
    if isinstance(value, list):
        value = value[0] if value else ""
    return str(value if value is not None else "").strip().lower() not in ("false", "0", "off", "no")


def canonical_roles(roles: Any) -> list[str]:
    """The known roles of a link, in OpenCRQ's order; none known reads as ``unknown`` (inherits everything)."""
    given = {str(r).strip().lower() for r in (roles or []) if str(r).strip()}
    known = [role for role in LINK_ROLE_INHERITANCE if role in given]
    return known or ["unknown"]


def inherited_needs(roles: list[str]) -> dict[str, str]:
    """The strongest inheritance of each security need over *roles* (OpenCRQ ``inheritedCriteriaOfRoles``)."""
    result = dict.fromkeys(SECURITY_NEEDS, "none")
    for role in roles:
        for need, level in LINK_ROLE_INHERITANCE.get(role, {}).items():
            if _INHERITANCE_ORDER.index(level) > _INHERITANCE_ORDER.index(result[need]):
                result[need] = level
    return result


def _key(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


_APOSTROPHES = re.compile("['\u2018\u2019\u02bc`\u00b4]")
_SEPARATORS = re.compile(r"[\s\-_.]+")


def loose_key(value: Any) -> str:
    """The second, looser name key: apostrophes dropped, hyphen, underscore, dot and spaces one separator.

    Read only when the exact key, hostname and FQDN find nothing (``Jamies-MacBook-Air`` vs
    ``Jamie's MacBook Air``, ``MacBook-Pro-de-Nicolas`` vs ``MacBook Pro de Nicolas``).
    """
    return _SEPARATORS.sub(" ", _APOSTROPHES.sub("", str(value or "").casefold())).strip()


def _url_host(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return urlsplit(text if "//" in text else f"//{text}").hostname or ""
    except ValueError:
        return ""


@dataclass
class WantedLink:
    """One OpenCRQ ``support_to_primary`` link as the sync wants it stored."""

    external_id: str
    crown_jewel_key: str
    record_id: uuid.UUID
    roles: list[str]
    inherited_needs: dict[str, str]
    #: ``opencrq`` for a link a person made in OpenCRQ, ``rule`` for one a Dependency Rule made (#1591 S3).
    source: str = "opencrq"


#: The link sources this sync writes and owns.
SYNC_SOURCES: tuple[str, ...] = ("opencrq", "rule")


@dataclass
class LinkPlan:
    """What the link sync writes, keeps and could not place."""

    add: list[WantedLink] = field(default_factory=list)
    update: list[WantedLink] = field(default_factory=list)
    delete: list[uuid.UUID] = field(default_factory=list)
    #: (primary asset, supporting asset) of a link a person removed: not added back.
    kept_removed: list[tuple[str, str]] = field(default_factory=list)
    #: (primary asset, supporting asset) with no inventory record to link.
    unmatched: list[tuple[str, str]] = field(default_factory=list)
    #: (primary asset, supporting asset) whose name matches several records.
    ambiguous: list[tuple[str, str]] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "added": len(self.add),
            "updated": len(self.update),
            "removed": len(self.delete),
            "kept_removed_by_a_person": [{"primary_asset": p, "supporting_asset": s} for p, s in self.kept_removed],
            "not_in_inventory": [{"primary_asset": p, "supporting_asset": s} for p, s in self.unmatched[:50]],
            "not_in_inventory_count": len(self.unmatched),
            "ambiguous": [{"primary_asset": p, "supporting_asset": s} for p, s in self.ambiguous[:50]],
            "ambiguous_count": len(self.ambiguous),
        }


def _match(
    names: list[str], index: dict[str, list[uuid.UUID]], asset_of: dict[uuid.UUID, uuid.UUID]
) -> tuple[uuid.UUID | None, bool]:
    """``(record, ambiguous)`` of *names* in *index*, one candidate per merged asset.

    The records are grouped by their asset (a record with no asset is its own);
    one asset is a match, linked through its preferred record (the smallest
    id, deterministic); two or more assets are ambiguous.
    """
    by_asset: dict[uuid.UUID, set[uuid.UUID]] = {}
    for name in names:
        for record_id in index.get(name, []) if name else []:
            by_asset.setdefault(asset_of.get(record_id, record_id), set()).add(record_id)
    if len(by_asset) > 1:
        return None, True
    if not by_asset:
        return None, False
    return min(next(iter(by_asset.values())), key=str), False


def plan_links(
    jewel_names: dict[str, str],
    links: list[dict[str, Any]],
    records_by_opencrq_id: dict[str, uuid.UUID],
    records_by_name: dict[str, list[uuid.UUID]],
    stored: list[OrganizationCrownJewelLink],
    *,
    records_by_loose_name: dict[str, list[uuid.UUID]] | None = None,
    asset_of: dict[uuid.UUID, uuid.UUID] | None = None,
) -> LinkPlan:
    """Plan the synced links (no I/O).

    *jewel_names* maps an OpenCRQ primary asset id to the profile row's name;
    *links* are OpenCRQ ``support_to_primary`` links (``id``,
    ``primaryAssetId``, ``supportingAssetId``, ``supportingAssetName``,
    ``roles``); *stored* the organization's links of source ``opencrq``
    (removed ones included). A manual link is not in *stored* and never touched.
    *records_by_name* holds the exact keys (name, hostname, FQDN),
    *records_by_loose_name* the :func:`loose_key` ones, *asset_of* the merged
    asset of each record: candidates count per asset, never per record.
    """
    plan = LinkPlan()
    assets = asset_of or {}
    loose = records_by_loose_name or {}
    by_external = {row.external_id: row for row in stored if row.external_id}
    seen: set[str] = set()
    for link in links:
        link_id = str(link.get("id") or "")
        primary_id = str(link.get("primaryAssetId") or "")
        jewel = jewel_names.get(primary_id)
        if not link_id or not jewel or link_id in seen:
            continue
        seen.add(link_id)
        supporting_id = str(link.get("supportingAssetId") or "")
        supporting_name = str(link.get("supportingAssetName") or link.get("supportingAssetDerivedName") or "").strip()
        label = supporting_name or supporting_id
        record_id = records_by_opencrq_id.get(supporting_id)
        if record_id is None and supporting_name:
            exact = [_key(supporting_name), _key(_url_host(supporting_name))]
            record_id, ambiguous = _match(exact, records_by_name, assets)
            if record_id is None and not ambiguous:
                record_id, ambiguous = _match([loose_key(supporting_name)], loose, assets)
            if ambiguous:
                plan.ambiguous.append((jewel, label))
                continue
        if record_id is None:
            plan.unmatched.append((jewel, label))
            continue
        roles = canonical_roles(link.get("roles"))
        source = "rule" if str(link.get("source") or "") == "rule" or link.get("ruleId") else "opencrq"
        wanted = WantedLink(link_id, _key(jewel)[:200], record_id, roles, inherited_needs(roles), source)
        current = by_external.get(link_id)
        # Another record of the same merged asset already carries the link: kept, no churn.
        if (
            current is not None
            and current.record_id
            and current.record_id != record_id
            and assets.get(current.record_id, current.record_id) == assets.get(record_id, record_id)
        ):
            record_id = wanted.record_id = current.record_id
        if current is None:
            plan.add.append(wanted)
        elif current.removed:
            plan.kept_removed.append((jewel, label))
        elif (
            current.record_id != record_id
            or current.crown_jewel_key != wanted.crown_jewel_key
            or list(current.roles or []) != roles
            or current.source != source
        ):
            plan.update.append(wanted)
    plan.delete = [row.id for row in stored if row.external_id not in seen and not row.removed]
    return plan


async def apply_links(db: AsyncSession, organization_id: uuid.UUID, plan: LinkPlan) -> int:
    """Write *plan*; answers the rows written or removed."""
    if plan.delete:
        await db.execute(
            delete(OrganizationCrownJewelLink).where(
                OrganizationCrownJewelLink.organization_id == organization_id,
                OrganizationCrownJewelLink.id.in_(plan.delete),
            )
        )
    for wanted in plan.update:
        row = (
            (
                await db.execute(
                    select(OrganizationCrownJewelLink).where(
                        OrganizationCrownJewelLink.organization_id == organization_id,
                        OrganizationCrownJewelLink.source.in_(SYNC_SOURCES),
                        OrganizationCrownJewelLink.external_id == wanted.external_id,
                    )
                )
            )
            .scalars()
            .first()
        )
        if row is not None:
            row.record_id = wanted.record_id
            row.crown_jewel_key = wanted.crown_jewel_key
            row.roles = wanted.roles
            row.inherited_needs = wanted.inherited_needs
            row.source = wanted.source
    for wanted in plan.add:
        db.add(
            OrganizationCrownJewelLink(
                organization_id=organization_id,
                crown_jewel_key=wanted.crown_jewel_key,
                record_id=wanted.record_id,
                source=wanted.source,
                external_id=wanted.external_id,
                roles=wanted.roles,
                inherited_needs=wanted.inherited_needs,
            )
        )
    await db.flush()
    return len(plan.add) + len(plan.update) + len(plan.delete)


async def _read_links(
    base_url: str, headers: Any, tenant: str, primary_ids: list[str]
) -> tuple[list[dict[str, Any]], str | None]:
    from app.integrations.providers.opencrq._common import _trpc

    links: list[dict[str, Any]] = []
    for primary_id in primary_ids:
        items, err = await _trpc(
            base_url,
            headers,
            "assetDependencies.getSupportToPrimaryLinksByPrimaryAssetId",
            "list primary asset links",
            input={"primaryAssetId": primary_id},
            tenant_id=tenant,
        )
        if err:
            return links, str(err)
        for item in items or []:
            if isinstance(item, dict):
                links.append({**item, "primaryAssetId": item.get("primaryAssetId") or primary_id})
        if len(links) >= MAX_LINKS:
            break
    return links[:MAX_LINKS], None


async def sync_links(
    db: AsyncSession, organization_id: uuid.UUID, *, base_url: str, headers: Any, tenant: str, jewels: list[Any]
) -> tuple[LinkPlan | None, str | None]:
    """Read the links of the profile's OpenCRQ primary assets and write them; ``(plan, error)``.

    A failed read writes nothing: a link is removed only when OpenCRQ answered.
    """
    jewel_names = {
        str(j.external_id): j.name for j in jewels if getattr(j, "source", "") == "opencrq" and j.external_id
    }
    if not jewel_names:
        return LinkPlan(), None
    links, err = await _read_links(base_url, headers, tenant, list(jewel_names))
    if err:
        return None, err
    rows = (
        await db.execute(
            select(
                OrganizationAssetRecord.id,
                OrganizationAssetRecord.source,
                OrganizationAssetRecord.external_id,
                OrganizationAssetRecord.asset_id,
                OrganizationAssetRecord.name,
                OrganizationAssetRecord.name_key,
                OrganizationAssetRecord.fields["hostname"].astext,
                OrganizationAssetRecord.fields["fqdn"].astext,
                OrganizationAssetRecord.fields["url"].astext,
            ).where(OrganizationAssetRecord.organization_id == organization_id)
        )
    ).all()
    by_opencrq: dict[str, uuid.UUID] = {}
    by_name: dict[str, list[uuid.UUID]] = {}
    by_loose: dict[str, list[uuid.UUID]] = {}
    asset_of: dict[uuid.UUID, uuid.UUID] = {}
    for record_id, source, external_id, asset_id, name, name_key, hostname, fqdn, url in rows:
        if source == "opencrq" and external_id:
            by_opencrq[str(external_id)] = record_id
        if asset_id is not None:
            asset_of[record_id] = asset_id
        # #1591 S3: an OpenCRQ supporting asset named by a device's hostname (Intune, Defender) or a
        # service's FQDN matches it too; every key once per record.
        for key in {_key(name_key), _key(hostname), _key(fqdn), _key(_url_host(url))} - {""}:
            by_name.setdefault(key, []).append(record_id)
        for key in {loose_key(name), loose_key(hostname), loose_key(fqdn)} - {""}:
            by_loose.setdefault(key, []).append(record_id)
    stored = list(
        (
            await db.execute(
                select(OrganizationCrownJewelLink).where(
                    OrganizationCrownJewelLink.organization_id == organization_id,
                    OrganizationCrownJewelLink.source.in_(SYNC_SOURCES),
                )
            )
        ).scalars()
    )
    plan = plan_links(
        jewel_names, links, by_opencrq, by_name, stored, records_by_loose_name=by_loose, asset_of=asset_of
    )
    await apply_links(db, organization_id, plan)
    return plan, None


async def sync_links_for(
    db: AsyncSession, *, user_id: Any, filters: dict[str, Any] | None, jewels: list[Any]
) -> dict[str, Any] | None:
    """After an OpenCRQ primary-asset sync saved *jewels*: the link import, when its option is on.

    Answers the plan summary (``None`` when the option is off), ``error`` set
    when OpenCRQ or the organization could not be read; never raises.
    """
    if not link_option_on(filters):
        return None
    from app.integrations.providers.opencrq._common import _resolve_tenant
    from app.services.organization_context_opencrq import _opencrq_connection
    from app.services.organizations import get_primary

    primary = await get_primary(db)
    if primary is None:
        return {"error": "no organization"}
    connection = await _opencrq_connection(db, str(user_id))
    if isinstance(connection, str):
        return {"error": f"OpenCRQ: {connection}"[:500]}
    base_url, headers = connection
    tenant, err = await _resolve_tenant(base_url, headers, str((filters or {}).get("tenant_id") or ""))
    if err:
        return {"error": f"OpenCRQ: {err}"[:500]}
    plan, err = await sync_links(db, primary.id, base_url=base_url, headers=headers, tenant=tenant or "", jewels=jewels)
    if err or plan is None:
        return {"error": f"OpenCRQ links: {err}"[:500]}
    return plan.summary()
