# SPDX-License-Identifier: Apache-2.0

"""The primary assets a CTEM signal reaches through the asset inventory (#1591, S3).

The scope gate (``ctem_scope_gate.decide``) reads criticality from the
organization profile; this module adds the path the profile alone cannot
follow: software named by a signal -> the inventory assets that run it (the
asset's software list, the software an endpoint source such as Intune,
Defender or OpenAEV reports per device, and the operating system family the
device reports) -> the asset groups they belong to -> the primary assets
(crown jewels) the record or the group supports through
``organization_crown_jewel_links`` (sources ``manual``, ``opencrq``, ``rule``;
a person's removal of a synced link is a tombstone and reaches nothing).

:func:`load_index` reads it once per organization (only the assets that
reach a primary asset are read, so the cost follows the links, not the
fleet); :meth:`PrimaryAssetIndex.reach` is pure and answers each primary
asset reached with its chain (software -> asset or group -> primary asset,
role and source), the decision record's evidence.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

#: The order a link source is shown in when several reach the same primary asset: a person's first.
SOURCE_ORDER: tuple[str, ...] = ("manual", "rule", "opencrq")
#: Records read at most as members of a linked group (a larger group is cut, said in ``truncated``).
MAX_MEMBERS = 20000
#: Installed software rows read at most for the assets that reach a primary asset.
MAX_SOFTWARE_ROWS = 200000


def _key(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


def product_matches(product: str, ref: str) -> bool:
    """An installed *product* named by a software reference *ref* (a name, or a CPE's product part).

    The same rule as the gate's tech stack match: equal, or *product* as a whole
    word of *ref* (``Windows`` in ``windows_10``, never ``win`` in ``windows``).
    """
    p, r = str(product or "").strip().lower(), str(ref or "").strip().lower()
    if not p or not r:
        return False
    if r.startswith("cpe:"):
        parts = r.split(":")
        r = parts[4] if len(parts) > 4 else r
    r = r.replace("_", " ")
    p = p.replace("_", " ")
    return p == r or re.search(rf"(^|[^a-z0-9]){re.escape(p)}($|[^a-z0-9])", r) is not None


@dataclass(frozen=True)
class Link:
    """One link of a record or a group to a primary asset."""

    jewel_key: str
    source: str
    roles: tuple[str, ...] = ()
    via: str = ""


@dataclass
class Reacher:
    """An inventory asset that reaches at least one primary asset, with what it runs."""

    name: str
    keys: set[str] = field(default_factory=set)
    #: product key -> product label (installed software, the asset's own list, the OS family).
    products: dict[str, str] = field(default_factory=dict)
    #: (link, through kind ``asset`` or ``asset_group``, through name)
    links: list[tuple[Link, str, str]] = field(default_factory=list)


@dataclass
class PrimaryAssetIndex:
    """The assets that reach a primary asset, read once per organization."""

    loaded: bool = False
    reachers: list[Reacher] = field(default_factory=list)
    truncated: bool = False
    _cache: dict[str, frozenset[int]] = field(default_factory=dict, repr=False)

    def _running(self, ref: str) -> frozenset[int]:
        """Indexes of the reachers running software *ref*, cached per reference."""
        hit = self._cache.get(ref)
        if hit is None:
            hit = frozenset(
                i
                for i, r in enumerate(self.reachers)
                if any(product_matches(label, ref) or product_matches(ref, label) for label in r.products.values())
            )
            self._cache[ref] = hit
        return hit

    def _matching(self, software: list[str], keys: list[str] | None) -> list[tuple[Reacher, str]]:
        """The reachers *software* or subject *keys* name, each with the software reference it runs."""
        wanted = {k for k in (keys or []) if k}
        out: list[tuple[Reacher, str]] = []
        for i, reacher in enumerate(self.reachers):
            matched = next((ref for ref in software if ref and i in self._running(ref)), "")
            if matched or wanted & reacher.keys:
                out.append((reacher, matched))
        return out

    def reach(self, software: list[str], keys: list[str] | None = None) -> list[dict[str, Any]]:
        """The primary assets *software* (names, CPEs, tech stack products) or subject *keys* reach.

        One entry per (primary asset key, through, source), best source first:
        ``{primary_asset_key, software, asset, through, through_kind, source, roles, via}``.
        """
        if not self.reachers:
            return []
        found: dict[tuple[str, str, str], dict[str, Any]] = {}
        for reacher, matched in self._matching(software, keys):
            for link, kind, through in reacher.links:
                key = (link.jewel_key, through, link.source)
                if key in found:
                    continue
                found[key] = {
                    "primary_asset_key": link.jewel_key,
                    "software": matched,
                    "asset": reacher.name,
                    "through": through,
                    "through_kind": kind,
                    "source": link.source,
                    "roles": list(link.roles),
                    "via": link.via,
                }
        rank = {s: n for n, s in enumerate(SOURCE_ORDER)}
        return sorted(
            found.values(), key=lambda h: (h["primary_asset_key"], rank.get(h["source"], 9), h["through"], h["asset"])
        )

    def supporting(self, software: list[str], keys: list[str] | None, jewel_key: str) -> list[dict[str, Any]]:
        """Every inventory asset *software* or *keys* name that reaches *jewel_key*, one entry per asset.

        Unlike :meth:`reach`, which keeps one chain per (through, source), this lists each
        supporting asset, its best link first: ``{asset, software, through, through_kind,
        source, roles}``, sorted by asset name. The loop trace pages it (#1979).
        """
        rank = {s: n for n, s in enumerate(SOURCE_ORDER)}
        out: list[dict[str, Any]] = []
        for reacher, matched in self._matching(software, keys):
            links = [(link, kind, through) for link, kind, through in reacher.links if link.jewel_key == jewel_key]
            if not links:
                continue
            link, kind, through = min(links, key=lambda e: (rank.get(e[0].source, 9), e[1] != "asset", e[2]))
            out.append(
                {
                    "asset": reacher.name,
                    "software": matched,
                    "through": through,
                    "through_kind": kind,
                    "source": link.source,
                    "roles": sorted({r for lk, _, _ in links for r in lk.roles}),
                }
            )
        return sorted(out, key=lambda e: (e["asset"].casefold(), e["asset"]))

    def totals(self, software: list[str], keys: list[str] | None = None) -> dict[str, dict[str, int]]:
        """Per primary asset key, how many inventory assets reach it: ``{assets, direct, via_group}``.

        The decision record stores these next to the first chains, so a primary asset
        reached by 10,000 workstations reads 10,000, never the five chains kept (#1979).
        """
        out: dict[str, dict[str, int]] = {}
        for reacher, _ in self._matching(software, keys):
            kinds: dict[str, set[str]] = {}
            for link, kind, _through in reacher.links:
                kinds.setdefault(link.jewel_key, set()).add(kind)
            for jewel_key, seen in kinds.items():
                row = out.setdefault(jewel_key, {"assets": 0, "direct": 0, "via_group": 0})
                row["assets"] += 1
                if "asset" in seen:
                    row["direct"] += 1
                else:
                    row["via_group"] += 1
        return out


def _record_products(fields: dict[str, Any]) -> dict[str, str]:
    """What a record says it runs: its own software list and the OS family a device reports."""
    out: dict[str, str] = {}
    for item in fields.get("tech_stack") or []:
        label = str(item or "").strip()
        if label:
            out.setdefault(_key(label), label)
    os_family = str(fields.get("os_family") or "").strip()
    if os_family:
        out.setdefault(_key(os_family), os_family)
    return out


async def load_index(db: AsyncSession, organization_id: uuid.UUID) -> PrimaryAssetIndex:
    """Read the assets of *organization_id* that reach a primary asset. Never raises on empty data."""
    from app.models.organization_asset import OrganizationAssetSoftware
    from app.models.organization_asset_record import (
        OrganizationAssetGroup,
        OrganizationAssetGroupMember,
        OrganizationAssetRecord,
        OrganizationCrownJewelLink,
    )

    link = OrganizationCrownJewelLink
    rows = (
        await db.execute(
            select(link.record_id, link.group_id, link.crown_jewel_key, link.source, link.roles, link.via).where(
                link.organization_id == organization_id, link.removed.is_(False)
            )
        )
    ).all()
    index = PrimaryAssetIndex(loaded=True)
    if not rows:
        return index
    record_links: dict[uuid.UUID, list[tuple[Link, str, str]]] = {}
    group_links: dict[uuid.UUID, list[Link]] = {}
    for record_id, group_id, jewel_key, source, roles, via in rows:
        item = Link(str(jewel_key), str(source or "manual"), tuple(str(r) for r in roles or ()), str(via or ""))
        if record_id is not None:
            record_links.setdefault(record_id, []).append((item, "asset", ""))
        elif group_id is not None:
            group_links.setdefault(group_id, []).append(item)
    if group_links:
        grp, member = OrganizationAssetGroup, OrganizationAssetGroupMember
        group_rows = (
            await db.execute(
                select(grp.id, grp.name).where(grp.organization_id == organization_id, grp.id.in_(list(group_links)))
            )
        ).all()
        names: dict[uuid.UUID, str] = {gid: str(name) for gid, name in group_rows}
        members = (
            await db.execute(
                select(member.group_id, member.record_id)
                .where(member.group_id.in_(list(group_links)))
                .limit(MAX_MEMBERS + 1)
            )
        ).all()
        if len(members) > MAX_MEMBERS:
            index.truncated, members = True, members[:MAX_MEMBERS]
        for group_id, record_id in members:
            for item in group_links[group_id]:
                record_links.setdefault(record_id, []).append((item, "asset_group", str(names.get(group_id) or "")))
    rec = OrganizationAssetRecord
    records = (
        await db.execute(
            select(rec.id, rec.asset_id, rec.name, rec.external_id, rec.fields).where(
                rec.organization_id == organization_id, rec.id.in_(list(record_links))
            )
        )
    ).all()
    by_asset: dict[Any, Reacher] = {}
    asset_ids: list[uuid.UUID] = []
    for record_id, asset_id, name, external_id, fields in records:
        if asset_id is not None and asset_id not in by_asset:
            asset_ids.append(asset_id)
        holder = by_asset.setdefault(asset_id or record_id, Reacher(name=str(name)))
        holder.keys |= {k for k in (_key(name), _key(external_id), _key((fields or {}).get("hostname"))) if k}
        holder.products.update(_record_products(dict(fields or {})))
        for item, kind, through in record_links.get(record_id, []):
            entry = (item, kind, through or str(name))
            if entry not in holder.links:
                holder.links.append(entry)
    if asset_ids:
        soft = OrganizationAssetSoftware
        installed = (
            await db.execute(
                select(soft.asset_id, soft.product_key, soft.product)
                .where(soft.organization_id == organization_id, soft.asset_id.in_(asset_ids))
                .distinct()
                .limit(MAX_SOFTWARE_ROWS + 1)
            )
        ).all()
        if len(installed) > MAX_SOFTWARE_ROWS:
            index.truncated, installed = True, installed[:MAX_SOFTWARE_ROWS]
        for asset_id, product_key, product in installed:
            owner = by_asset.get(asset_id)
            if owner is not None and product:
                owner.products.setdefault(str(product_key or _key(product)), str(product))
    index.reachers = [r for r in by_asset.values() if r.links]
    return index


async def asset_primary_assets(
    db: AsyncSession, organization_id: uuid.UUID, asset_id: uuid.UUID
) -> list[dict[str, Any]]:
    """The primary assets an inventory asset supports, with the role and how (#1591, S3).

    Each entry: ``{primary_asset, through, through_kind, source, roles, via}``;
    the agent tools and the asset page read it.
    """
    from app.models.organization_asset_record import (
        OrganizationAssetGroup,
        OrganizationAssetGroupMember,
        OrganizationAssetRecord,
        OrganizationCrownJewelLink,
    )

    rec, link, member, grp = (
        OrganizationAssetRecord,
        OrganizationCrownJewelLink,
        OrganizationAssetGroupMember,
        OrganizationAssetGroup,
    )
    direct = (
        await db.execute(
            select(link.crown_jewel_key, link.source, link.roles, link.via, rec.name)
            .join(rec, rec.id == link.record_id)
            .where(link.organization_id == organization_id, rec.asset_id == asset_id, link.removed.is_(False))
        )
    ).all()
    grouped = (
        await db.execute(
            select(link.crown_jewel_key, link.source, link.roles, link.via, grp.name)
            .join(grp, grp.id == link.group_id)
            .join(member, member.group_id == grp.id)
            .join(rec, rec.id == member.record_id)
            .where(link.organization_id == organization_id, rec.asset_id == asset_id, link.removed.is_(False))
        )
    ).all()
    out: dict[tuple[str, str, str], dict[str, Any]] = {}
    for kind, rows in (("asset", direct), ("asset_group", grouped)):
        for jewel_key, source, roles, via, through in rows:
            out.setdefault(
                (str(jewel_key), str(through), str(source)),
                {
                    "primary_asset": str(jewel_key),
                    "through": str(through),
                    "through_kind": kind,
                    "source": str(source),
                    "roles": [str(r) for r in roles or ()],
                    "via": str(via or ""),
                },
            )
    return sorted(out.values(), key=lambda e: (e["primary_asset"], e["through_kind"], e["through"]))
