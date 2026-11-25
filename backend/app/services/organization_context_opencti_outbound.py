# SPDX-License-Identifier: Apache-2.0

"""Outbound ``assets`` sync to OpenCTI: assets as Systems, their software as Software (#1982 S3).

The STIX model is the one OpenCTI's own rule ``vulnerability_software_to_system``
reads, so a vulnerability OpenCTI knows on a software reaches the systems that
run it:

- an XTM One asset is a ``System`` (an Identity), typed with the OpenAEV
  taxonomy of OpenCTI-Platform/opencti#19943 when the OpenCTI carries it
  (category, subcategory, criticality, exposure, environment, owner, platform);
- a software it runs is a ``Software`` observable (STIX 2.1 SCO: name, vendor,
  version, CPE), one per vendor, product and version;
- the installation is a ``uses`` relationship from the System to the Software.

Only what this sync created is updated and deleted, keyed in
``OrganizationContextSync.outbound_ids`` (``system:<name>``,
``software:<vendor>|<product>|<version>``, ``uses:<name>|<software key>``); a
System or relationship of OpenCTI's connectors is never touched. A Software
observable is never deleted: OpenCTI deduplicates observables on their STIX id,
so the one this sync created may be the one a connector reports too; a software
no asset runs any more loses its ``uses`` relationships and leaves the keys.
An asset OpenCTI wrote (source ``opencti``) is never pushed back. The outbound
filters select the assets the way an import's select the Systems it reads
(``outbound_row_matches``); an asset leaving them is deleted from OpenCTI when
this sync created its System.

:func:`plan_opencti_outbound` is the pure rule; :func:`run_opencti_outbound`
reads only the objects this sync created and writes with the owner's token.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.organization_context import OrganizationProfile
from app.services.organization_context_outbound import OutboundPlan, outbound_row_matches

#: The relationship of an installation: System ``uses`` Software (OpenCTI's propagation rule reads it).
INSTALLATION_RELATIONSHIP = "uses"
#: The description of a System this sync creates (kept as OpenCTI holds it on an update).
SYSTEM_DESCRIPTION = "Pushed by XTM One from its organization inventory."
#: The most installations one run pushes; past it the run writes nothing and says so.
MAX_INSTALLATIONS = 5000
#: Asset field -> the System attribute of OpenCTI-Platform/opencti#19943 it writes (taxonomy only).
SYSTEM_FIELDS: dict[str, str] = {
    "category": "x_opencti_system_category",
    "subcategory": "x_opencti_system_subcategory",
    "criticality": "x_opencti_criticality",
    "internet_facing": "x_opencti_internet_facing",
    "environment": "x_opencti_environment",
    "owner": "x_opencti_owner",
    "os_family": "x_opencti_platform",
}
#: Values that mean "not said": never written, so OpenCTI's own value stays.
_UNSET = frozenset({"", "unknown"})

SYSTEM_PREFIX = "system:"
SOFTWARE_PREFIX = "software:"
USES_PREFIX = "uses:"


def _fold(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


def system_key(name: str) -> str:
    return f"{SYSTEM_PREFIX}{_fold(name)}"


def software_key(vendor: str, product: str, version: str) -> str:
    return f"{SOFTWARE_PREFIX}{_fold(vendor)}|{_fold(product)}|{_fold(version)}"


def uses_key(asset_name: str, software: str) -> str:
    return f"{USES_PREFIX}{_fold(asset_name)}|{software.removeprefix(SOFTWARE_PREFIX)}"


def system_body(asset: Any, *, taxonomy: bool) -> dict[str, Any]:
    """The OpenCTI System input of *asset*: its name, and with *taxonomy* the #19943 attributes it states."""
    body: dict[str, Any] = {"name": str(asset.name)}
    if not taxonomy:
        return body
    for field_name, attribute in SYSTEM_FIELDS.items():
        value = str(getattr(asset, field_name, "") or "").strip()
        if value.casefold() not in _UNSET:
            body[attribute] = value
    return body


def software_body(entry: Mapping[str, Any]) -> dict[str, Any]:
    """The OpenCTI Software observable input of one installation (STIX 2.1: name, vendor, version, cpe)."""
    body: dict[str, Any] = {"name": str(entry.get("product") or "").strip()}
    for field_name in ("vendor", "version", "cpe"):
        value = str(entry.get(field_name) or "").strip()
        if value and value.casefold() != "unknown":
            body[field_name] = value
    return body


def _patch(body: dict[str, Any], current: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The ``fieldPatch`` edits that bring *current* to *body* (only the attributes *body* states)."""
    return [
        {"key": key, "value": [value]}
        for key, value in body.items()
        if str(current.get(key) or "").strip().casefold() != str(value).strip().casefold()
    ]


def plan_opencti_outbound(
    profile: OrganizationProfile,
    installed: Mapping[str, Iterable[Mapping[str, Any]]],
    current: Mapping[str, Mapping[str, Any]],
    outbound_ids: Mapping[str, str],
    filters: dict[str, Any] | None = None,
    *,
    taxonomy: bool = True,
) -> OutboundPlan:
    """XTM One assets and their software -> OpenCTI Systems, Software and ``uses`` (#1982 S3, no I/O).

    *installed* maps an asset's folded name to its installations (vendor, product, version, cpe);
    *current* maps each id of *outbound_ids* OpenCTI still holds to the object as read (an id it no
    longer holds is absent: its key is forgotten and the object created again).
    """
    plan = OutboundPlan(platform="opencti")
    wanted: set[str] = set()
    for asset in getattr(profile, "assets_exposure", None) or []:
        if str(asset.source or "").strip().casefold() == "opencti":
            plan.skipped.append(asset.name)
            continue
        if not outbound_row_matches(asset, filters):
            continue
        key = system_key(asset.name)
        if key in wanted:
            continue
        wanted.add(key)
        body = system_body(asset, taxonomy=taxonomy)
        system_id = outbound_ids.get(key, "")
        node = current.get(system_id) if system_id else None
        if node is None:
            plan.create.append((asset.name, asset.criticality))
            plan.bodies[asset.name] = {**body, "description": SYSTEM_DESCRIPTION}
        elif edits := _patch(body, node):
            plan.update.append((system_id, asset.name, asset.criticality))
            plan.patches[asset.name] = edits
        for entry in installed.get(_fold(asset.name), ()):
            soft = software_body(entry)
            if not soft["name"]:
                continue
            s_key = software_key(soft.get("vendor", ""), soft["name"], soft.get("version", ""))
            r_key = uses_key(asset.name, s_key)
            if s_key not in wanted:
                wanted.add(s_key)
                s_id = outbound_ids.get(s_key, "")
                if not (s_id and s_id in current):
                    plan.software.append((s_key, soft))
            if r_key in wanted:
                continue
            wanted.add(r_key)
            r_id = outbound_ids.get(r_key, "")
            if not (r_id and r_id in current):
                plan.relations.append((r_key, key, s_key))
    deleted_systems: set[str] = set()
    for key, object_id in outbound_ids.items():
        if key in wanted or object_id not in current:
            continue
        if key.startswith(SYSTEM_PREFIX):
            plan.delete.append((key, object_id))
            deleted_systems.add(key.removeprefix(SYSTEM_PREFIX))
    for key, object_id in outbound_ids.items():
        if key in wanted or object_id not in current or not key.startswith(USES_PREFIX):
            continue
        # A System deleted takes its relationships with it: only the others are deleted one by one.
        if key.removeprefix(USES_PREFIX).split("|", 1)[0] not in deleted_systems:
            plan.relations_delete.append((key, object_id))
    plan.released = sorted(
        k for k, object_id in outbound_ids.items() if k.startswith(SOFTWARE_PREFIX) and k not in wanted
    )
    return plan


# --------------------------------------------------------------------------- OpenCTI reads and writes

_READ = (
    "query XtmOutboundRead($filters: FilterGroup) {\n"
    "  systems(first: 500, filters: $filters) { edges { node { id name %s } } }\n"
    "  stixCyberObservables(first: 500, filters: $filters) { edges { node { id entity_type } } }\n"
    "  stixCoreRelationships(first: 500, filters: $filters) { edges { node { id relationship_type } } }\n"
    "}\n"
)


def _ids_filter(ids: list[str]) -> dict[str, Any]:
    return {
        "mode": "and",
        "filters": [{"key": ["id"], "values": ids, "operator": "eq", "mode": "or"}],
        "filterGroups": [],
    }


async def _taxonomy(gql: Any) -> tuple[bool, str | None]:
    from app.services.organization_context_import import _OCTI_SYSTEM_PROBE, opencti_lacks_system_taxonomy

    _, err = await gql(_OCTI_SYSTEM_PROBE, None)
    if not err:
        return True, None
    return (False, None) if opencti_lacks_system_taxonomy(err) else (False, err)


async def _read_current(gql: Any, ids: list[str], taxonomy: bool) -> tuple[dict[str, dict[str, Any]], str | None]:
    """The objects of *ids* OpenCTI still holds, by id (500 at a time)."""
    attributes = " ".join(SYSTEM_FIELDS.values()) if taxonomy else ""
    found: dict[str, dict[str, Any]] = {}
    for start in range(0, len(ids), 500):
        data, err = await gql(_READ % attributes, {"filters": _ids_filter(ids[start : start + 500])})
        if err:
            return found, err
        for root in ("systems", "stixCyberObservables", "stixCoreRelationships"):
            for edge in (data.get(root) or {}).get("edges") or []:
                node = edge.get("node") or {}
                if node.get("id"):
                    found[str(node["id"])] = node
    return found, None


async def _installations(
    db: AsyncSession, organization_id: uuid.UUID | None
) -> tuple[dict[str, list[dict[str, Any]]], int]:
    """Asset folded name -> its installations from the asset store (``(map, count)``)."""
    from sqlalchemy import select

    from app.models.organization_asset import OrganizationAsset, OrganizationAssetSoftware

    if organization_id is None:
        return {}, 0
    rows = (
        await db.execute(
            select(
                OrganizationAsset.name,
                OrganizationAssetSoftware.vendor,
                OrganizationAssetSoftware.product,
                OrganizationAssetSoftware.version,
                OrganizationAssetSoftware.cpe,
            )
            .join(OrganizationAsset, OrganizationAsset.id == OrganizationAssetSoftware.asset_id)
            .where(OrganizationAssetSoftware.organization_id == organization_id)
            .limit(MAX_INSTALLATIONS + 1)
        )
    ).all()
    out: dict[str, list[dict[str, Any]]] = {}
    for name, vendor, product, version, cpe in rows[:MAX_INSTALLATIONS]:
        out.setdefault(_fold(name), []).append({"vendor": vendor, "product": product, "version": version, "cpe": cpe})
    return out, len(rows)


def _profile_installations(profile: OrganizationProfile) -> dict[str, list[dict[str, Any]]]:
    """The software an asset row names (``products``), for an organization whose store holds none."""
    return {
        _fold(asset.name): [{"product": product} for product in asset.products]
        for asset in getattr(profile, "assets_exposure", None) or []
        if asset.products
    }


async def run_opencti_outbound(
    db: AsyncSession, sync: Any, ids: dict[str, str], *, user_id: uuid.UUID, apply: bool
) -> tuple[OutboundPlan, dict[str, str]]:
    """Plan and, with *apply*, write the assets to OpenCTI with *user_id*'s token (#1982 S3)."""
    from app.services import organization_context as org_context
    from app.services.organization_opencti import opencti_gql

    gql = await opencti_gql(db, str(user_id))
    if isinstance(gql, str):
        return OutboundPlan(platform="opencti", error=f"OpenCTI: {gql}"), ids
    taxonomy, err = await _taxonomy(gql)
    if err:
        return OutboundPlan(platform="opencti", error=f"OpenCTI: {err}"[:500]), ids
    current: dict[str, dict[str, Any]] = {}
    if ids:
        current, err = await _read_current(gql, sorted(set(ids.values())), taxonomy)
    if err:
        return OutboundPlan(platform="opencti", error=f"OpenCTI: {err}"[:500]), ids
    row = await org_context.current_version(db)
    profile = await org_context.hydrated_profile(db, row)
    installed, count = await _installations(db, getattr(row, "organization_id", None))
    if count > MAX_INSTALLATIONS:
        return (
            OutboundPlan(
                platform="opencti",
                error=f"OpenCTI: more than {MAX_INSTALLATIONS} installations to push; narrow the filters, nothing written",
            ),
            ids,
        )
    if not installed:
        installed = _profile_installations(profile)
    plan = plan_opencti_outbound(
        profile, installed, current, ids, getattr(sync, "filters", None) or {}, taxonomy=taxonomy
    )
    for key in [k for k, v in ids.items() if v not in current]:
        ids.pop(key, None)  # gone from OpenCTI: forgotten, created again by this plan
    if not apply:
        return plan, ids
    return plan, await _apply(gql, plan, ids)


async def _apply(gql: Any, plan: OutboundPlan, ids: dict[str, str]) -> dict[str, str]:
    errors: list[str] = []
    for name, _ in plan.create:
        data, err = await gql(
            "mutation XtmSystemAdd($input: SystemAddInput!) { systemAdd(input: $input) { id } }",
            {"input": plan.bodies[name]},
        )
        new_id = str((data.get("systemAdd") or {}).get("id") or "") if not err else ""
        if not new_id:
            errors.append(f"{name}: {err or 'no id answered'}")
            continue
        ids[system_key(name)] = new_id
    for system_id, name, _ in plan.update:
        _, err = await gql(
            "mutation XtmSystemPatch($id: ID!, $input: [EditInput]!) { stixDomainObjectEdit(id: $id) "
            "{ fieldPatch(input: $input) { id } } }",
            {"id": system_id, "input": plan.patches[name]},
        )
        if err:
            errors.append(f"{name}: {err}")
    for key, soft in plan.software:
        data, err = await gql(
            "mutation XtmSoftwareAdd($type: String!, $software: SoftwareAddInput) "
            "{ stixCyberObservableAdd(type: $type, Software: $software) { id } }",
            {"type": "Software", "software": soft},
        )
        new_id = str((data.get("stixCyberObservableAdd") or {}).get("id") or "") if not err else ""
        if not new_id:
            errors.append(f"{soft['name']}: {err or 'no id answered'}")
            continue
        ids[key] = new_id
    for key, from_key, to_key in plan.relations:
        from_id, to_id = ids.get(from_key), ids.get(to_key)
        if not (from_id and to_id):
            continue  # its System or Software failed above: reported there
        data, err = await gql(
            "mutation XtmUsesAdd($input: StixCoreRelationshipAddInput!) "
            "{ stixCoreRelationshipAdd(input: $input) { id } }",
            {"input": {"fromId": from_id, "toId": to_id, "relationship_type": INSTALLATION_RELATIONSHIP}},
        )
        new_id = str((data.get("stixCoreRelationshipAdd") or {}).get("id") or "") if not err else ""
        if not new_id:
            errors.append(f"{key}: {err or 'no id answered'}")
            continue
        ids[key] = new_id
    for key, object_id in plan.relations_delete:
        _, err = await gql(
            "mutation XtmUsesDelete($id: ID!) { stixCoreRelationshipEdit(id: $id) { delete } }", {"id": object_id}
        )
        if err:
            errors.append(f"delete {key}: {err}")
        else:
            ids.pop(key, None)
    for key, object_id in plan.delete:
        _, err = await gql(
            "mutation XtmSystemDelete($id: ID!) { stixDomainObjectEdit(id: $id) { delete } }", {"id": object_id}
        )
        if err:
            errors.append(f"delete {key}: {err}")
            continue
        ids.pop(key, None)
        prefix = f"{USES_PREFIX}{key.removeprefix(SYSTEM_PREFIX)}|"
        for gone in [k for k in ids if k.startswith(prefix)]:
            ids.pop(gone, None)  # deleted with its System
    for key in plan.released:
        ids.pop(key, None)  # no asset runs it any more: the observable stays, shared by its STIX id
    plan.error = "; ".join(errors)[:500]
    return ids
