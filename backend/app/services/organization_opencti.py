# SPDX-License-Identifier: Apache-2.0

"""OpenCTI identity and PIRs of the tracked organizations (#9157, slice M3).

Design: ``dev-docs/ctem-fleets.md`` section 9.4, decision D2 = A.

* **Identity**: a tracked organization links to an existing OpenCTI
  ``Organization`` the administrator picks among :func:`search_identities`
  (name and alias); a missing one is created only on the administrator's
  explicit confirmation (:func:`link_identity` with ``create``).
  ``x_opencti_organization_type`` is set only when OpenCTI holds none
  (:data:`ORGANIZATION_TYPE_BY_RELATIONSHIP`), never overwritten: the
  relationship of record lives in XTM One.
* **PIRs**: one ``THREAT_CUSTOM`` PIR per linked tracked organization of
  criticality ``high`` or ``critical``, named ``XTM One - <organization> -
  third-party``; the other linked ones share :data:`SHARED_PIR_NAME`. A
  PIR's criteria are ``targets`` towards the organization's OpenCTI identity
  (weight 3), towards its sector (weight 1) and towards its regions (weight
  1). Its id is saved in the organization's profile ``pir_ids`` (a new
  version, source ``api``), so the trigger filter of section 8.3 works per
  organization unchanged.

:func:`plan_pirs` is the pure rule; :func:`sync_pirs` reads OpenCTI with the
company-managed integration and a token minted for the administrator, and
writes only when ``apply`` is set. OpenCTI cannot edit a PIR's criteria
(``pirFieldPatch`` takes name, description and rescan period), so a PIR whose
criteria changed is deleted and created again. Nothing is guessed: a sector
or region OpenCTI holds no entity for is reported, an organization without
an OpenCTI identity gets no PIR and is reported.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

Gql = Callable[[str, dict[str, Any] | None], Awaitable[tuple[dict[str, Any], str | None]]]

#: XTM One relationship -> OpenCTI ``x_opencti_organization_type`` (set only when empty).
ORGANIZATION_TYPE_BY_RELATIONSHIP: dict[str, str] = {
    "provider": "vendor",
    "partner": "partner",
    "subsidiary": "constituent",
    "customer": "other",
}
#: Criticalities that get a PIR of their own (D2 = A); the others share one.
OWN_PIR_CRITICALITIES = ("high", "critical")
PIR_NAME_PREFIX = "XTM One"
SHARED_PIR_NAME = f"{PIR_NAME_PREFIX} - third parties"
PIR_TYPE = "THREAT_CUSTOM"
PIR_RESCAN_DAYS = 30
RELATIONSHIP_TYPE = "targets"
IDENTITY_WEIGHT = 3
CONTEXT_WEIGHT = 1


def own_pir_name(organization_name: str) -> str:
    return f"{PIR_NAME_PREFIX} - {organization_name} - third-party"


def _criterion(weight: int, to_ids: list[str]) -> dict[str, Any]:
    """One criterion in the canonical shape the OpenCTI form writes (``entity_type`` + ``toId``)."""
    return {
        "weight": weight,
        "filters": {
            "mode": "and",
            "filterGroups": [],
            "filters": [
                {"key": ["entity_type"], "values": [RELATIONSHIP_TYPE], "operator": "eq", "mode": "or"},
                {"key": ["toId"], "values": list(to_ids), "operator": "eq", "mode": "or"},
            ],
        },
    }


def pir_filters(min_confidence: int) -> dict[str, Any]:
    """The PIR's scope: confidence at least *min_confidence* (no filter at 0)."""
    filters = []
    if min_confidence > 0:
        filters.append({"key": ["confidence"], "values": [str(min_confidence)], "operator": "gte", "mode": "or"})
    return {"mode": "and", "filterGroups": [], "filters": filters}


@dataclass
class TrackedTarget:
    """What a tracked organization contributes to a PIR, resolved in OpenCTI."""

    organization_id: str
    name: str
    criticality: str
    identity_id: str
    sector_ids: list[str] = field(default_factory=list)
    region_ids: list[str] = field(default_factory=list)


@dataclass
class PlannedPir:
    name: str
    organization_ids: list[str]
    criteria: list[dict[str, Any]]

    def signature(self) -> list[tuple[int, tuple[str, ...]]]:
        return criteria_signature(self.criteria)


def criteria_signature(criteria: list[dict[str, Any]]) -> list[tuple[int, tuple[str, ...]]]:
    """Criteria compared by weight and target ids, order-free."""
    out: list[tuple[int, tuple[str, ...]]] = []
    for c in criteria or []:
        filters = c.get("filters")
        if isinstance(filters, str):
            import json

            try:
                filters = json.loads(filters)
            except ValueError:
                filters = {}
        to_ids: list[str] = []
        for f in (filters or {}).get("filters") or []:
            key = f.get("key")
            keys = key if isinstance(key, list) else [key]
            if "toId" in keys:
                to_ids.extend(str(v) for v in f.get("values") or [])
        out.append((int(c.get("weight") or 0), tuple(sorted(set(to_ids)))))
    return sorted(out)


def _criteria_of(targets: list[TrackedTarget]) -> list[dict[str, Any]]:
    identities = sorted({t.identity_id for t in targets})
    sectors = sorted({s for t in targets for s in t.sector_ids})
    regions = sorted({r for t in targets for r in t.region_ids})
    criteria = [_criterion(IDENTITY_WEIGHT, identities)]
    if sectors:
        criteria.append(_criterion(CONTEXT_WEIGHT, sectors))
    if regions:
        criteria.append(_criterion(CONTEXT_WEIGHT, regions))
    return criteria


def plan_pirs(targets: list[TrackedTarget]) -> list[PlannedPir]:
    """The PIRs the tracked organizations need (D2 = A), no I/O."""
    planned: list[PlannedPir] = []
    shared: list[TrackedTarget] = []
    for target in sorted(targets, key=lambda t: t.name.lower()):
        if target.criticality in OWN_PIR_CRITICALITIES:
            planned.append(PlannedPir(own_pir_name(target.name), [target.organization_id], _criteria_of([target])))
        else:
            shared.append(target)
    if shared:
        planned.append(PlannedPir(SHARED_PIR_NAME, [t.organization_id for t in shared], _criteria_of(shared)))
    return planned


@dataclass
class PirSyncReport:
    created: list[dict[str, Any]] = field(default_factory=list)  # {name, pir_id}
    replaced: list[dict[str, Any]] = field(default_factory=list)  # {name, pir_id, previous_pir_id}
    unchanged: list[dict[str, Any]] = field(default_factory=list)  # {name, pir_id}
    deleted: list[dict[str, Any]] = field(default_factory=list)  # {name, pir_id}: a PIR of ours no organization needs
    not_linked: list[str] = field(default_factory=list)  # organizations without an OpenCTI identity
    unresolved: list[dict[str, Any]] = field(default_factory=list)  # {organization, regions, sectors} OpenCTI lacks
    profiles_updated: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "created": self.created,
            "replaced": self.replaced,
            "unchanged": self.unchanged,
            "deleted": self.deleted,
            "not_linked": self.not_linked,
            "unresolved": self.unresolved,
            "profiles_updated": self.profiles_updated,
        }


# --------------------------------------------------------------------------- OpenCTI reads and writes

_ORG_FIELDS = "id name x_opencti_aliases x_opencti_organization_type"


async def search_identities(gql: Gql, search: str, *, limit: int = 20) -> list[dict[str, Any]]:
    """OpenCTI Organizations matching *search* by name or alias, for the administrator to pick."""
    data, err = await gql(
        f"query XtmOrgSearch($search: String, $first: Int) {{ organizations(first: $first, search: $search) "
        f"{{ edges {{ node {{ {_ORG_FIELDS} }} }} }} }}",
        {"search": search, "first": max(1, min(limit, 50))},
    )
    if err:
        raise RuntimeError(err)
    nodes = [e.get("node") or {} for e in ((data.get("organizations") or {}).get("edges") or [])]
    wanted = search.strip().lower()

    def exact(n: dict[str, Any]) -> bool:
        names = [n.get("name"), *(n.get("x_opencti_aliases") or [])]
        return wanted in {str(v).strip().lower() for v in names if v}

    return sorted((n for n in nodes if n.get("id")), key=lambda n: (not exact(n), str(n.get("name") or "").lower()))


async def read_identity(gql: Gql, identity_id: str) -> dict[str, Any] | None:
    """The OpenCTI Organization *identity_id*, or None when OpenCTI holds none."""
    data, err = await gql(
        f"query XtmOrg($id: String!) {{ organization(id: $id) {{ {_ORG_FIELDS} }} }}", {"id": identity_id}
    )
    if err:
        raise RuntimeError(err)
    return data.get("organization") or None


async def link_identity(
    gql: Gql, *, name: str, relationship: str, identity_id: str = "", create: bool = False
) -> tuple[str, str | None]:
    """``(identity id, organization type written or None)``: the picked Organization, or a new one on *create*.

    The type is written only when OpenCTI holds none; an existing type is never overwritten.
    """
    wanted_type = ORGANIZATION_TYPE_BY_RELATIONSHIP.get(relationship or "")
    if identity_id:
        node = await read_identity(gql, identity_id)
        if node is None:
            # An entity created in an OpenCTI draft is not readable outside it: such an id is refused, never
            # stored as linked; it links once the draft is approved.
            raise LookupError(
                f"OpenCTI holds no Organization {identity_id} outside a draft. If it was created in an OpenCTI "
                "draft, approve that draft first, then link it again; nothing was linked."
            )
        if node.get("x_opencti_organization_type") or not wanted_type:
            return node["id"], None
        _, err = await gql(
            "mutation XtmOrgType($id: ID!, $input: [EditInput]!) { stixDomainObjectEdit(id: $id) "
            "{ fieldPatch(input: $input) { id } } }",
            {"id": node["id"], "input": [{"key": "x_opencti_organization_type", "value": [wanted_type]}]},
        )
        if err:
            raise RuntimeError(err)
        return node["id"], wanted_type
    if not create:
        raise ValueError("pick an OpenCTI Organization, or confirm its creation")
    inp: dict[str, Any] = {"name": name}
    if wanted_type:
        inp["x_opencti_organization_type"] = wanted_type
    data, err = await gql(
        "mutation XtmOrgAdd($input: OrganizationAddInput!) { organizationAdd(input: $input) { id } }", {"input": inp}
    )
    if err:
        raise RuntimeError(err)
    created = (data.get("organizationAdd") or {}).get("id")
    if not created:
        raise RuntimeError("OpenCTI did not return the new Organization's id")
    return created, wanted_type


async def _our_pirs(gql: Gql) -> dict[str, dict[str, Any]]:
    """The PIRs this module owns, by name: ``{name: {id, criteria}}``."""
    data, err = await gql(
        "query XtmPirs($search: String) { pirs(first: 200, search: $search) "
        "{ edges { node { id name pir_criteria { weight filters } } } } }",
        {"search": PIR_NAME_PREFIX},
    )
    if err:
        raise RuntimeError(err)
    out: dict[str, dict[str, Any]] = {}
    for edge in (data.get("pirs") or {}).get("edges") or []:
        node = edge.get("node") or {}
        name = str(node.get("name") or "")
        if node.get("id") and (
            name == SHARED_PIR_NAME or (name.startswith(f"{PIR_NAME_PREFIX} - ") and name.endswith(" - third-party"))
        ):
            out[name] = {"id": node["id"], "criteria": node.get("pir_criteria") or []}
    return out


async def _create_pir(gql: Gql, pir: PlannedPir, min_confidence: int) -> str:
    data, err = await gql(
        "mutation XtmPirAdd($input: PirAddInput!) { pirAdd(input: $input) { id } }",
        {
            "input": {
                "name": pir.name,
                "pir_type": PIR_TYPE,
                "description": "Created by XTM One for its tracked organizations (third-party risk).",
                "pir_rescan_days": PIR_RESCAN_DAYS,
                "pir_criteria": pir.criteria,
                "pir_filters": pir_filters(min_confidence),
            }
        },
    )
    if err:
        raise RuntimeError(err)
    pir_id = (data.get("pirAdd") or {}).get("id")
    if not pir_id:
        raise RuntimeError(f"OpenCTI did not return the id of PIR {pir.name!r}")
    return str(pir_id)


async def _delete_pir(gql: Gql, pir_id: str) -> None:
    _, err = await gql("mutation XtmPirDelete($id: ID!) { pirDelete(id: $id) }", {"id": pir_id})
    if err:
        raise RuntimeError(err)


async def resolve_targets(
    gql: Gql, organizations: list[Any], profiles: dict[str, Any]
) -> tuple[list[TrackedTarget], PirSyncReport]:
    """Each linked tracked organization with its sector and region entities; the rest reported."""
    from app.services.opencti_trigger_provisioning import _resolve_country_ids, _resolve_sector_ids

    report = PirSyncReport()
    targets: list[TrackedTarget] = []
    for o in organizations:
        if o.role != "tracked" or o.archived_at is not None:
            continue
        if not o.opencti_organization_id:
            report.not_linked.append(o.name)
            continue
        profile = profiles.get(str(o.id))
        sectors = [profile.sector] if profile is not None and profile.sector else []
        regions = list(profile.regions) if profile is not None else []
        sector_ids, missing_sectors = await _resolve_sector_ids(gql, sectors) if sectors else ([], [])
        region_ids, missing_regions = await _resolve_country_ids(gql, regions) if regions else ([], [])
        if missing_sectors or missing_regions:
            report.unresolved.append({"organization": o.name, "sectors": missing_sectors, "regions": missing_regions})
        targets.append(
            TrackedTarget(
                organization_id=str(o.id),
                name=o.name,
                criticality=o.criticality,
                identity_id=o.opencti_organization_id,
                sector_ids=sector_ids,
                region_ids=region_ids,
            )
        )
    return targets, report


async def reconcile_pirs(
    gql: Gql, planned: list[PlannedPir], report: PirSyncReport, *, apply: bool, min_confidence: int
) -> dict[str, str]:
    """Create, replace or keep each planned PIR; delete ours no plan needs. ``{organization id: pir id}``."""
    existing = await _our_pirs(gql)
    by_org: dict[str, str] = {}
    for pir in planned:
        current = existing.pop(pir.name, None)
        if current is not None and criteria_signature(current["criteria"]) == pir.signature():
            report.unchanged.append({"name": pir.name, "pir_id": current["id"]})
            pir_id = current["id"]
        elif not apply:
            entry = {"name": pir.name, "pir_id": None}
            if current is not None:
                report.replaced.append({**entry, "previous_pir_id": current["id"]})
            else:
                report.created.append(entry)
            continue
        else:
            if current is not None:
                await _delete_pir(gql, current["id"])
            pir_id = await _create_pir(gql, pir, min_confidence)
            if current is not None:
                report.replaced.append({"name": pir.name, "pir_id": pir_id, "previous_pir_id": current["id"]})
            else:
                report.created.append({"name": pir.name, "pir_id": pir_id})
        for org_id in pir.organization_ids:
            by_org[org_id] = pir_id
    for name, stale in existing.items():
        if apply:
            await _delete_pir(gql, stale["id"])
        report.deleted.append({"name": name, "pir_id": stale["id"]})
    return by_org


# --------------------------------------------------------------------------- the connection and the run


async def opencti_gql(db: AsyncSession, user_id: str) -> Gql | str:
    """A GraphQL caller on the company-managed OpenCTI with a token for *user_id*, or why not."""
    from sqlalchemy import select

    from app.api.v1.helpers import integration_visible_filter
    from app.integrations.providers.opencti._common import _client, _graphql, _request_headers
    from app.models.integration import Integration
    from app.services.integration_tools import _get_token

    integration = (
        (
            await db.execute(
                select(Integration)
                .where(
                    Integration.provider == "opencti",
                    # access_restricted: honoured, integration_visible_filter below decides the administrator's reach.
                    Integration.is_company_managed.is_(True),
                    await integration_visible_filter(db, user_id),
                )
                .order_by(Integration.created_at)
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    if integration is None:
        return "no company-managed OpenCTI integration is registered"
    from app.services.module_identity import platform_is_registered, service_account_refusal

    registered = await platform_is_registered(db, "opencti")
    if (refusal := service_account_refusal("opencti", integration, user_id, registered=registered)) is not None:
        return refusal
    token = await _get_token(db, str(integration.id), "opencti", user_id=user_id, integ=integration)
    if not token or "||" not in token:
        return "no OpenCTI token could be minted for this administrator"
    base_url, _, api_token = token.partition("||")
    base_url = base_url.strip().rstrip("/")
    headers = _request_headers(
        {"Authorization": f"Bearer {api_token.strip()}", "Content-Type": "application/json"}, None
    )

    async def gql(query: str, variables: dict[str, Any] | None = None) -> tuple[dict[str, Any], str | None]:
        async with _client(headers, timeout=30) as client:
            return await _graphql(client, base_url, headers, query, variables)

    return gql


async def sync_pirs(
    db: AsyncSession, gql: Gql, *, user_id: uuid.UUID, apply: bool, min_confidence: int = 0
) -> PirSyncReport:
    """Plan the PIRs of the tracked organizations; with *apply* write them and save each ``pir_ids``."""
    from app.services import organization_context as org_context
    from app.services import organizations as orgs

    organizations = await orgs.list_organizations(db)
    profiles: dict[str, Any] = {}
    for o in organizations:
        if o.role == "tracked":
            row = await org_context.current_version(db, o.id)
            profiles[str(o.id)] = orgs.parse_tracked_profile(row.profile if row else {})
    targets, report = await resolve_targets(gql, organizations, profiles)
    by_org = await reconcile_pirs(gql, plan_pirs(targets), report, apply=apply, min_confidence=min_confidence)
    if not apply:
        return report
    for o in organizations:
        if o.role != "tracked":
            continue
        profile = profiles[str(o.id)]
        wanted = [by_org[str(o.id)]] if str(o.id) in by_org else []
        if profile.pir_ids != wanted:
            await org_context.save_version(
                db,
                profile.model_copy(update={"pir_ids": wanted}),
                source="api",
                author_user_id=user_id,
                organization_id=o.id,
            )
            report.profiles_updated.append(o.name)
    return report
