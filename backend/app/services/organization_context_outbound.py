# SPDX-License-Identifier: Apache-2.0

"""Outbound syncs of the organization profile: XTM One is the master (#1134 S3).

A sync has one direction and one master, never a free two-way merge:

- ``inbound`` (the default): the platform is the master; its rows are
  read-only in the profile (``organization_context_import``);
- ``outbound``: the XTM One list (typed, CSV, API or agent-maintained) is the
  master; the platform rows this sync CREATED follow it (created, updated,
  deleted to match). ``OrganizationContextSync.outbound_ids`` holds them,
  keyed on the XTM One row; a platform row the sync did not create is never
  touched, except the criticality of a crown jewel already linked to it by
  ``opencrq_asset_id`` (the existing jewel -> OpenCRQ criticality push).

:data:`DIRECTIONS` is the support matrix read from the platform APIs the
providers call today; a pair without ``outbound`` names why. :func:`plan_outbound`
is the pure rule; :func:`run_outbound` writes it with the owner's token, like
an inbound run.

Since #1501 an AI system is an asset of category ``ai_target``: an outbound
``assets`` sync to ``openaev`` pushes the AI target assets XTM One owns (typed,
CSV, API or agent rows) to OpenAEV's ``/api/ai_targets``; endpoints and every
other category stay inbound only (:func:`plan_ai_target_outbound`).

Since #1982 S1 an outbound ``asset_groups`` sync to ``openaev`` pushes the asset
groups XTM One owns to OpenAEV's ``/api/asset_groups``, their members resolved to
the OpenAEV assets they are linked to (:func:`plan_asset_group_outbound`), and an
outbound sync's ``filters`` select the rows it pushes the way an import's select
the rows it reads (:func:`outbound_row_matches`): a row leaving the filter is
deleted from the platform only when this sync created it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.organization_context import OrganizationProfile, is_platform_source

INBOUND = "inbound"
OUTBOUND = "outbound"
SYNC_DIRECTIONS: tuple[str, ...] = (INBOUND, OUTBOUND)

#: (section, source) -> the directions supported, and why outbound is not (empty when it is).
DIRECTIONS: dict[tuple[str, str], tuple[tuple[str, ...], str]] = {
    # #1591: the rows are OpenCRQ PRIMARY assets, whose security needs OpenCRQ owns; the old push wrote
    # them as supporting assets (the wrong layer), so the pairing reads only until a primary-asset write
    # is wired. A saved outbound sync of it answers this reason and writes nothing.
    ("crown_jewels", "opencrq"): (
        (INBOUND,),
        "OpenCRQ owns its primary assets and their security needs: the sync reads them (#1591).",
    ),
    # #1501: outbound pushes the AI target assets only; OpenAEV endpoints are owned by its agents.
    ("assets", "openaev"): ((INBOUND, OUTBOUND), ""),
    # #1982 S1: the groups XTM One owns, members linked to OpenAEV assets; OpenAEV's own groups are never touched.
    ("asset_groups", "openaev"): ((INBOUND, OUTBOUND), ""),
    ("pirs", "opencti"): ((INBOUND,), "Planned: OpenCTI PIR writes are not wired for syncs yet."),
    ("assets", "opencti"): ((INBOUND,), "OpenCTI Systems are owned by its connectors."),
    ("software", "openaev"): ((INBOUND,), "OpenAEV software inventory is read from its agents."),
}

#: Supporting assets an outbound run creates with: a crown jewel carries no OpenCRQ category.
OUTBOUND_CATEGORY = "generic_asset"
#: The sections whose outbound run writes OpenAEV AI targets (#1501).
AI_TARGET_SECTIONS: frozenset[str] = frozenset({"assets"})
#: The sections whose outbound run writes OpenAEV asset groups (#1982 S1).
ASSET_GROUP_SECTIONS: frozenset[str] = frozenset({"asset_groups"})
#: The OpenAEV fields of an asset group a PUT copies whole: an update sends OpenAEV's own values back.
_ASSET_GROUP_KEPT_FIELDS: tuple[str, ...] = ("asset_group_tags", "asset_group_dynamic_filter")
#: Outbound filter key -> the row attribute it reads (#1982 S1); a key the row does not carry never excludes it.
OUTBOUND_FILTER_FIELDS: dict[str, str] = {
    "category": "category",
    "subcategory": "subcategory",
    "criticality": "criticality",
    "internet_facing": "internet_facing",
    "environment": "environment",
    "platform": "os_family",
    "source": "source",
}
#: The provider an AI target created from XTM One carries when the row names none (OpenAEV requires one).
DEFAULT_AI_TARGET_PROVIDER = "custom_http"
#: The fields of an OpenAEV AI target the asset row is the master of; every other field of the
#: OpenAEV row (description, tags, system prompt, configuration, token) is sent back as read.
_AI_TARGET_MASTER_FIELDS: tuple[str, ...] = (
    "asset_name",
    "asset_criticality",
    "ai_target_provider",
    "ai_target_endpoint",
    "ai_target_model",
    "ai_target_modality",
)
#: The OpenAEV fields a PUT copies whole (BeanUtils): an update that lacks one would erase it.
_AI_TARGET_KEPT_FIELDS: tuple[str, ...] = (
    "asset_description",
    "asset_external_reference",
    "asset_tags",
    "ai_target_system_prompt",
    "ai_target_configuration",
    "ai_target_token",
)


def directions(section: str, source: str) -> tuple[tuple[str, ...], str]:
    """The directions *section* from *source* supports, and why outbound is not (empty when it is)."""
    return DIRECTIONS.get((section, source), ((INBOUND,), "This pairing reads only."))


def supports(section: str, source: str, direction: str) -> bool:
    return direction in directions(section, source)[0]


@dataclass
class OutboundPlan:
    """What an outbound run writes on the platform; nothing in the profile changes."""

    create: list[tuple[str, str]] = field(default_factory=list)  # (name, criticality)
    update: list[tuple[str, str, str]] = field(default_factory=list)  # (asset id, name, criticality)
    delete: list[tuple[str, str]] = field(default_factory=list)  # (key, asset id)
    skipped: list[str] = field(default_factory=list)  # rows a platform owns (never pushed back)
    error: str = ""
    #: The platform written: ``opencrq`` (crown jewels) or ``openaev`` (AI targets, #1501).
    platform: str = "opencrq"
    #: AI targets: the OpenAEV body of each created or updated row, keyed on the row's name.
    bodies: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: Asset groups (#1982 S1): the OpenAEV asset ids each created or updated group holds, keyed on its name.
    members: dict[str, list[str]] = field(default_factory=dict)
    #: Asset groups: ``group name: member name`` of each member with no OpenAEV counterpart (never created).
    unlinked: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        ref = f"{self.platform}_asset_id"
        return {
            "create": [{"name": n, "criticality": c} for n, c in self.create],
            "update": [{ref: a, "name": n, "criticality": c} for a, n, c in self.update],
            "delete": [{"name": k, ref: a} for k, a in self.delete],
            "skipped": list(self.skipped),
            **({"unlinked_members": list(self.unlinked)} if self.unlinked else {}),
            "error": self.error,
        }


def _key(name: str) -> str:
    return name.strip().casefold()


def _filter_values(filters: dict[str, Any], key: str) -> list[str]:
    raw = filters.get(key)
    items = raw if isinstance(raw, list) else ([] if raw in (None, "") else [raw])
    return [str(v).strip().casefold() for v in items if str(v).strip()]


def outbound_row_matches(row: Any, filters: dict[str, Any] | None) -> bool:
    """Whether an XTM One *row* is pushed by an outbound sync whose ``filters`` are *filters* (#1982 S1, no I/O).

    The keys are the import step's, read on the XTM One row: ``tags`` (any of), ``category``,
    ``subcategory``, ``criticality``, ``internet_facing``, ``environment``, ``platform`` (the OS
    family), ``source`` (each any of, case-insensitive) and ``search`` (the name holds it). A key with
    no value, or one the row's kind does not carry (a group has no category), selects every row.
    """
    filters = filters or {}
    tags = set(_filter_values(filters, "tags"))
    if tags and not tags & {str(t).casefold() for t in (getattr(row, "tags", None) or [])}:
        return False
    for key, attribute in OUTBOUND_FILTER_FIELDS.items():
        wanted = _filter_values(filters, key)
        if wanted and hasattr(row, attribute) and str(getattr(row, attribute) or "").casefold() not in wanted:
            return False
    search = str(filters.get("search") or "").strip().casefold()
    return not search or search in str(getattr(row, "name", "") or "").casefold()


def plan_outbound(
    profile: OrganizationProfile, assets: list[dict[str, Any]], outbound_ids: dict[str, str]
) -> OutboundPlan:
    """Crown jewels -> OpenCRQ supporting assets, the profile the master (no I/O).

    A jewel whose source is a platform (``opencrq`` included) is the platform's
    row: never pushed back, listed in ``skipped``. A jewel linked by
    ``opencrq_asset_id`` keeps its asset's criticality equal to its own. A
    jewel neither linked nor created yet is created; an asset this sync created
    whose jewel is gone is deleted.
    """
    from app.services.organization_context_opencrq import TO_OPENCRQ, _asset_name

    by_id = {str(a.get("id")): a for a in assets if a.get("id")}
    plan = OutboundPlan()
    wanted: set[str] = set()
    for jewel in profile.crown_jewels:
        key = _key(jewel.name)
        criticality = TO_OPENCRQ[jewel.criticality]
        linked = (jewel.opencrq_asset_id or "").strip()
        if is_platform_source(jewel.source) and not linked:
            plan.skipped.append(jewel.name)
            continue
        # A linked jewel (an OpenCRQ row or a typed jewel linked by name) keeps the asset's own name:
        # only its criticality is the profile's.
        asset_id = linked or outbound_ids.get(key, "")
        wanted.add(key)
        asset = by_id.get(asset_id) if asset_id else None
        if asset is None:
            if linked:
                continue  # linked to an asset OpenCRQ no longer holds: reported by the reconcile, never recreated
            plan.create.append((jewel.name, criticality))
            continue
        if str(asset.get("criticality") or "").lower() != criticality or (
            not linked and _asset_name(asset) != jewel.name
        ):
            plan.update.append((asset_id, jewel.name if not linked else _asset_name(asset), criticality))
    for key, asset_id in outbound_ids.items():
        if key not in wanted and asset_id in by_id:
            plan.delete.append((key, asset_id))
    return plan


def _ai_target_body(row: Any, target: dict[str, Any] | None) -> dict[str, Any]:
    """The OpenAEV ``AiTargetInput`` of an AI target asset *row*; *target* (the OpenAEV row) fills the rest."""
    body: dict[str, Any] = {key: (target or {}).get(key) for key in _AI_TARGET_KEPT_FIELDS}
    body["asset_tags"] = [str(t) for t in (body.get("asset_tags") or []) if t]
    body["ai_target_configuration"] = body.get("ai_target_configuration") or {}
    body.update(
        {
            "asset_name": row.name,
            "asset_criticality": row.criticality.upper(),
            "ai_target_provider": (row.ai_target_provider or DEFAULT_AI_TARGET_PROVIDER).upper(),
            "ai_target_endpoint": row.ai_target_endpoint or None,
            "ai_target_model": row.ai_target_model or None,
            "ai_target_modality": (row.ai_target_modality or "text").upper(),
        }
    )
    return body


def _differs(body: dict[str, Any], target: dict[str, Any]) -> bool:
    def norm(value: Any) -> str:
        return str(value or "").strip().casefold()

    return any(norm(body.get(key)) != norm(target.get(key)) for key in _AI_TARGET_MASTER_FIELDS)


def plan_ai_target_outbound(
    profile: OrganizationProfile,
    targets: list[dict[str, Any]],
    outbound_ids: dict[str, str],
    filters: dict[str, Any] | None = None,
) -> OutboundPlan:
    """AI target assets -> OpenAEV AI targets, the profile the master (#1501, no I/O).

    The rows XTM One owns (typed, CSV, API, agent) are pushed; a row a platform wrote (an OpenAEV AI
    target read by an inbound sync) is OpenAEV's own and listed in ``skipped``. An AI target this sync
    created whose row is gone is deleted; OpenAEV's other AI targets are never touched. An update sends
    the OpenAEV row's description, tags, system prompt, configuration and token back as read: a PUT
    replaces every field.
    """
    from app.services.organization_context import ai_target_assets

    by_id = {str(t.get("asset_id") or t.get("id") or ""): t for t in targets}
    by_id.pop("", None)
    plan = OutboundPlan(platform="openaev")
    wanted: set[str] = set()
    for row in ai_target_assets(profile):
        if is_platform_source(row.source):
            plan.skipped.append(row.name)
            continue
        if not outbound_row_matches(row, filters):
            continue  # left the filter: deleted below when this sync created it
        key = _key(row.name)
        if key in wanted:
            continue
        wanted.add(key)
        asset_id = outbound_ids.get(key, "")
        target = by_id.get(asset_id) if asset_id else None
        body = _ai_target_body(row, target)
        if target is None:
            plan.create.append((row.name, body["asset_criticality"]))
            plan.bodies[row.name] = body
        elif _differs(body, target):
            plan.update.append((asset_id, row.name, body["asset_criticality"]))
            plan.bodies[row.name] = body
    for key, asset_id in outbound_ids.items():
        if key not in wanted and asset_id in by_id:
            plan.delete.append((key, asset_id))
    return plan


def _openaev_asset_ids(profile: OrganizationProfile) -> dict[str, str]:
    """Asset name (case-folded) -> the OpenAEV asset id of the record an OpenAEV sync wrote (#1982 S1)."""
    out: dict[str, str] = {}
    for asset in getattr(profile, "assets_exposure", None) or []:
        if asset.source == "openaev" and asset.external_id:
            out.setdefault(_key(asset.name), asset.external_id)
    return out


def plan_asset_group_outbound(
    profile: OrganizationProfile,
    groups: dict[str, dict[str, Any]],
    outbound_ids: dict[str, str],
    filters: dict[str, Any] | None = None,
) -> OutboundPlan:
    """XTM One asset groups -> OpenAEV asset groups, the profile the master (#1982 S1, no I/O).

    *groups* are the OpenAEV groups this sync created, read whole (``GET /api/asset_groups/{id}``)
    and keyed on their id. A group XTM One owns (typed, CSV, API, agent) is pushed with its
    description and its members, each resolved to the OpenAEV asset it is linked to: the member
    itself when an OpenAEV sync wrote it, else the OpenAEV record of the same name. A member with no
    OpenAEV counterpart is listed in ``unlinked``, never created as an endpoint. A group a platform
    wrote is listed in ``skipped``. A group this sync created whose row is gone or left the filter is
    deleted; OpenAEV's other groups are never touched. An update sends OpenAEV's tags and dynamic
    filter back as read: its PUT replaces every field (#1702).
    """
    from app.services.organization_context import groups_of

    plan = OutboundPlan(platform="openaev")
    linked = _openaev_asset_ids(profile)
    members_of: dict[str, list[str]] = {}
    unlinked_of: dict[str, list[str]] = {}
    for asset in getattr(profile, "assets_exposure", None) or []:
        for group in groups_of(profile, asset):
            asset_id = (
                asset.external_id if asset.source == "openaev" and asset.external_id else linked.get(_key(asset.name))
            )
            if asset_id:
                members_of.setdefault(_key(group.name), []).append(asset_id)
            else:
                unlinked_of.setdefault(_key(group.name), []).append(asset.name)
    wanted: set[str] = set()
    for group in getattr(profile, "asset_groups", None) or []:
        if is_platform_source(group.source):
            plan.skipped.append(group.name)
            continue
        if not outbound_row_matches(group, filters):
            continue  # left the filter: deleted below when this sync created it
        key = _key(group.name)
        if key in wanted:
            continue
        wanted.add(key)
        members = sorted(set(members_of.get(key, [])))
        plan.unlinked += [f"{group.name}: {name}" for name in sorted(set(unlinked_of.get(key, [])))]
        group_id = outbound_ids.get(key, "")
        current = groups.get(group_id) if group_id else None
        body: dict[str, Any] = {k: (current or {}).get(k) for k in _ASSET_GROUP_KEPT_FIELDS}
        body["asset_group_tags"] = [str(t) for t in (body.get("asset_group_tags") or []) if t]
        if body.get("asset_group_dynamic_filter") is None:
            body.pop("asset_group_dynamic_filter")
        body.update({"asset_group_name": group.name, "asset_group_description": group.description or None})
        if current is None:
            plan.create.append((group.name, group.criticality))
        else:
            stored = sorted({str(a) for a in (current.get("asset_group_assets") or []) if a})
            same = (
                str(current.get("asset_group_name") or "") == group.name
                and str(current.get("asset_group_description") or "") == (group.description or "")
                and stored == members
            )
            if same:
                continue
            plan.update.append((group_id, group.name, group.criticality))
        plan.bodies[group.name] = body
        plan.members[group.name] = members
    for key, group_id in outbound_ids.items():
        if key not in wanted and group_id in groups:
            plan.delete.append((key, group_id))
    return plan


async def _run_asset_group_outbound(
    db: AsyncSession, sync: Any, ids: dict[str, str], *, user_id: uuid.UUID, apply: bool
) -> tuple[OutboundPlan, dict[str, str]]:
    """The asset groups to OpenAEV (#1982 S1): plan and, with *apply*, write with *user_id*'s token.

    Only the groups this sync created are read (one GET each, whole, before any write); a group
    OpenAEV no longer holds is forgotten, never recreated under the same key without a new run.
    """
    from app.integrations.providers.openaev._common import _api
    from app.services import organization_context as org_context
    from app.services.ctem_metrics_collector import _user_creds

    creds = await _user_creds(db, str(user_id), "openaev")
    if creds is None:
        return OutboundPlan(platform="openaev", error="OpenAEV: not connected, or no token for this administrator"), ids
    base_url, headers = creds
    groups: dict[str, dict[str, Any]] = {}
    for key, group_id in list(ids.items()):
        resp, err = await _api(
            base_url, headers, "get", f"/api/asset_groups/{group_id}", "read asset group", retry_transient=True
        )
        if err and "HTTP 404" in err:
            ids.pop(key, None)  # deleted in OpenAEV: forgotten, the next plan creates it again
            continue
        if err or resp is None:
            return OutboundPlan(platform="openaev", error=f"OpenAEV asset group {group_id}: {err or 'no answer'}"), ids
        data = resp.json()
        if isinstance(data, dict):
            groups[group_id] = data
    current = await org_context.current_version(db)
    profile = await org_context.hydrated_profile(db, current)
    plan = plan_asset_group_outbound(profile, groups, ids, getattr(sync, "filters", None) or {})
    if not apply:
        return plan, ids

    errors: list[str] = []
    for name, _ in plan.create:
        resp, err = await _api(
            base_url, headers, "post", "/api/asset_groups", "create asset group", json=plan.bodies[name]
        )
        created = resp.json() if resp is not None and not err else {}
        group_id = str((created or {}).get("asset_group_id") or "") if isinstance(created, dict) else ""
        if err or not group_id:
            errors.append(f"{name}: {err or 'no id answered'}")
            continue
        ids[_key(name)] = group_id
        if plan.members[name]:
            _, err = await _api(
                base_url,
                headers,
                "put",
                f"/api/asset_groups/{group_id}/assets",
                "set asset group members",
                json={"asset_group_assets": plan.members[name]},
            )
            if err:
                errors.append(f"{name} members: {err}")
    for group_id, name, _ in plan.update:
        _, err = await _api(
            base_url, headers, "put", f"/api/asset_groups/{group_id}", "update asset group", json=plan.bodies[name]
        )
        if not err:
            _, err = await _api(
                base_url,
                headers,
                "put",
                f"/api/asset_groups/{group_id}/assets",
                "set asset group members",
                json={"asset_group_assets": plan.members[name]},
            )
        if err:
            errors.append(f"{name}: {err}")
    for key, group_id in plan.delete:
        _, err = await _api(base_url, headers, "delete", f"/api/asset_groups/{group_id}", "delete asset group")
        if err:
            errors.append(f"delete {key}: {err}")
        else:
            ids.pop(key, None)
    plan.error = "; ".join(errors)[:500]
    return plan, ids


async def _run_ai_target_outbound(
    db: AsyncSession, sync: Any, ids: dict[str, str], *, user_id: uuid.UUID, apply: bool
) -> tuple[OutboundPlan, dict[str, str]]:
    """The AI target assets to OpenAEV (#1501): plan and, with *apply*, write with *user_id*'s token."""
    from app.integrations.providers.openaev._common import _api
    from app.services import organization_context as org_context
    from app.services.ctem_metrics_collector import _user_creds
    from app.services.organization_context_import import _openaev_search

    creds = await _user_creds(db, str(user_id), "openaev")
    if creds is None:
        return OutboundPlan(platform="openaev", error="OpenAEV: not connected, or no token for this administrator"), ids
    base_url, headers = creds
    read = await _openaev_search(base_url, headers, "/api/ai_targets/search", None)
    if read.error:
        return OutboundPlan(platform="openaev", error=read.error), ids
    if read.truncated:
        return OutboundPlan(platform="openaev", error="OpenAEV: too many AI targets to compare; nothing written"), ids
    current = await org_context.current_version(db)
    plan = plan_ai_target_outbound(
        await org_context.hydrated_profile(db, current), read.rows, ids, getattr(sync, "filters", None) or {}
    )
    if not apply:
        return plan, ids

    errors: list[str] = []
    for name, _ in plan.create:
        resp, err = await _api(base_url, headers, "post", "/api/ai_targets", "create AI target", json=plan.bodies[name])
        created = resp.json() if resp is not None and not err else {}
        asset_id = str((created or {}).get("asset_id") or "") if isinstance(created, dict) else ""
        if err or not asset_id:
            errors.append(f"{name}: {err or 'no id answered'}")
        else:
            ids[_key(name)] = asset_id
    for asset_id, name, _ in plan.update:
        _, err = await _api(
            base_url, headers, "put", f"/api/ai_targets/{asset_id}", "update AI target", json=plan.bodies[name]
        )
        if err:
            errors.append(f"{name}: {err}")
    for key, asset_id in plan.delete:
        _, err = await _api(base_url, headers, "delete", f"/api/ai_targets/{asset_id}", "delete AI target")
        if err:
            errors.append(f"delete {key}: {err}")
        else:
            ids.pop(key, None)
    plan.error = "; ".join(errors)[:500]
    return plan, ids


async def run_outbound(
    db: AsyncSession, sync: Any, *, user_id: uuid.UUID, apply: bool
) -> tuple[OutboundPlan, dict[str, str]]:
    """Plan and, with *apply*, write an outbound sync; ``(plan, outbound_ids after the run)``.

    ``crown_jewels`` to ``opencrq``, the AI target assets and the asset groups to ``openaev``
    are wired (:data:`DIRECTIONS`). Reads and writes with *user_id*'s token; a failed read writes nothing.
    """
    from app.integrations.providers.opencrq._common import _resolve_tenant, _trpc
    from app.services import organization_context as org_context
    from app.services import organization_context_opencrq as ocrq

    ids = dict(getattr(sync, "outbound_ids", None) or {})
    if not supports(sync.section, sync.source, OUTBOUND):
        return OutboundPlan(error=directions(sync.section, sync.source)[1] or "outbound is not supported"), ids
    if sync.source == "openaev" and sync.section in AI_TARGET_SECTIONS:
        return await _run_ai_target_outbound(db, sync, ids, user_id=user_id, apply=apply)
    if sync.source == "openaev" and sync.section in ASSET_GROUP_SECTIONS:
        return await _run_asset_group_outbound(db, sync, ids, user_id=user_id, apply=apply)
    connection = await ocrq._opencrq_connection(db, str(user_id))
    if isinstance(connection, str):
        return OutboundPlan(error=connection), ids
    base_url, headers = connection
    tenant, err = await _resolve_tenant(base_url, headers, str((sync.filters or {}).get("tenant_id") or ""))
    if err:
        return OutboundPlan(error=str(err)), ids
    assets, err = await ocrq._list_assets(base_url, headers, tenant or "")
    if err:
        return OutboundPlan(error=err), ids
    current = await org_context.current_version(db)
    plan = plan_outbound(org_context.parse_profile(current.profile if current else {}), assets, ids)
    if not apply:
        return plan, ids

    errors: list[str] = []
    for name, criticality in plan.create:
        asset_id = str(uuid.uuid4())
        _, err = await _trpc(
            base_url,
            headers,
            "supportingAssets.createSupportingAsset",
            "create supporting asset",
            input={
                "id": asset_id,
                "name": name,
                "exposure": "unknown",
                "categoryId": OUTBOUND_CATEGORY,
                "criticality": criticality,
            },
            mutation=True,
            tenant_id=tenant or "",
        )
        if err:
            errors.append(f"{name}: {err}")
        else:
            ids[_key(name)] = asset_id
    for asset_id, name, criticality in plan.update:
        _, err = await _trpc(
            base_url,
            headers,
            "supportingAssets.updateSupportingAsset",
            "update supporting asset",
            input={"id": asset_id, "name": name, "criticality": criticality},
            mutation=True,
            tenant_id=tenant or "",
        )
        if err:
            errors.append(f"{name}: {err}")
    gone = [asset_id for _, asset_id in plan.delete]
    if gone:
        _, err = await _trpc(
            base_url,
            headers,
            "supportingAssets.deleteSupportingAssets",
            "delete supporting assets",
            input=gone,
            mutation=True,
            tenant_id=tenant or "",
        )
        if err:
            errors.append(f"delete: {err}")
        else:
            for key, _ in plan.delete:
                ids.pop(key, None)
    plan.error = "; ".join(errors)[:500]
    return plan, ids
