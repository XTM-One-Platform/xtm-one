# SPDX-License-Identifier: Apache-2.0

"""OpenCRQ primary-asset sync of the organization profile (#8987, slice 3d; #1591).

The profile's primary assets (the ``primary_assets`` section of
``services/organization_context.py``, named primary assets before #1591) are
OpenCRQ's PRIMARY assets: the business assets, rated by four security needs
(availability, integrity, confidentiality, traceability). OpenCRQ's
supporting assets are the IT underneath and are never rows of this section.
An administrator runs the reconcile on demand
(``POST /api/v1/organization-context/opencrq-sync``, a dry run unless
``apply`` is set):

- **link**: a row without ``opencrq_asset_id`` whose name matches one
  primary asset (case-insensitive, exactly one) gets that asset's id; a typed
  row stays typed (#1473);
- **OpenCRQ -> profile**: a primary asset no row names joins the profile by
  the import's rule (criticality = its highest security need), source
  ``opencrq`` keyed on its id;
- a row whose ``opencrq_asset_id`` OpenCRQ no longer holds is reported,
  never removed; a row an older build keyed on a SUPPORTING asset is left to
  the import, which supersedes it;
- nothing is written to OpenCRQ: a primary asset's security needs are its own.

:func:`plan_sync` is the pure rule; :func:`run_sync` reads OpenCRQ through
the company-managed integration with a token minted for the administrator
and, with ``apply``, saves the next profile version (source ``api``).
Nothing is guessed: an ambiguous name is reported.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.organization_context import OrganizationProfile, PrimaryAsset

#: Profile criticality -> OpenCRQ supporting-asset criticality.
TO_OPENCRQ: dict[str, str] = {"low": "low", "medium": "medium", "high": "high", "critical": "very_high"}
#: The default lowest security need that keeps an OpenCRQ primary asset: empty, every primary asset (#1591).
DEFAULT_MIN_CRITICALITY = ""
#: Supporting assets read at most (pages of 500).
MAX_ASSETS = 2000


@dataclass
class SyncPlan:
    """What the sync changes, on each side, and what it could not decide."""

    profile: OrganizationProfile
    linked: list[tuple[str, str]] = field(default_factory=list)  # (jewel, asset id)
    pushed: list[tuple[str, str, str]] = field(default_factory=list)  # (asset id, name, criticality)
    pulled: list[tuple[str, str]] = field(default_factory=list)  # (asset id, name)
    ambiguous: list[str] = field(default_factory=list)  # jewel names matching several assets
    stale: list[tuple[str, str]] = field(default_factory=list)  # (jewel, asset id OpenCRQ lacks)
    #: Synced jewels whose pushed criticality falls below the sync's threshold: kept as typed rows (#1473).
    released: list[tuple[str, str]] = field(default_factory=list)  # (jewel, asset id)
    #: The threshold the plan pulled with, and the assets read by OpenCRQ criticality (#1558): an empty
    #: plan over assets none of which reaches the threshold says so, never "already agree".
    min_criticality: str = DEFAULT_MIN_CRITICALITY
    by_criticality: dict[str, int] = field(default_factory=dict)

    @property
    def profile_changed(self) -> bool:
        return bool(self.linked or self.pulled or self.released)

    @property
    def in_sync(self) -> bool:
        """Whether the profile and OpenCRQ agree: nothing to link, push or pull, nothing undecided."""
        return not (self.linked or self.pushed or self.pulled or self.ambiguous or self.stale)

    def summary(self) -> dict[str, Any]:
        return {
            "linked": [{"primary_asset": j, "opencrq_asset_id": a} for j, a in self.linked],
            "pushed_to_opencrq": [{"opencrq_asset_id": a, "name": n, "criticality": c} for a, n, c in self.pushed],
            "pulled_from_opencrq": [{"opencrq_asset_id": a, "name": n} for a, n in self.pulled],
            "ambiguous": list(self.ambiguous),
            "not_in_opencrq": [{"primary_asset": j, "opencrq_asset_id": a} for j, a in self.stale],
            "kept_below_sync_threshold": [{"primary_asset": j, "opencrq_asset_id": a} for j, a in self.released],
            "in_sync": self.in_sync,
            "min_criticality": self.min_criticality,
            "assets_read": sum(self.by_criticality.values()),
            "by_criticality": dict(self.by_criticality),
        }


def _asset_name(asset: dict) -> str:
    return str(asset.get("name") or asset.get("derivedName") or "").strip()


def count_by_criticality(assets: list[dict[str, Any]]) -> dict[str, int]:
    """The supporting assets per OpenCRQ criticality, lowest first; any other value counts as ``unknown`` (#1558)."""
    from app.services.organization_context_import import OPENCRQ_LEVELS

    counts: dict[str, int] = {}
    for asset in assets:
        level = str(asset.get("criticality") or "").lower()
        key = level if level in OPENCRQ_LEVELS else "unknown"
        counts[key] = counts.get(key, 0) + 1
    order = ("unknown", *OPENCRQ_LEVELS)
    return {key: counts[key] for key in order if key in counts}


def plan_sync(
    profile: OrganizationProfile, assets: list[dict[str, Any]], min_criticality: str = DEFAULT_MIN_CRITICALITY
) -> SyncPlan:
    """Reconcile *profile*'s primary assets (primary assets) with OpenCRQ's PRIMARY *assets* (no I/O, #1591).

    A row with no OpenCRQ link whose name matches exactly one primary asset is
    linked (a typed row stays typed, #1473); a primary asset no row names is
    pulled by the import's rule (:func:`organization_context_import.opencrq_primary_asset`);
    a linked row OpenCRQ no longer holds is reported. Nothing is pushed: a
    primary asset's rating is its four security needs, owned by OpenCRQ.
    """
    from app.services.organization_context_import import OPENCRQ_LEVELS, opencrq_primary_asset, primary_asset_level

    if min_criticality and min_criticality not in OPENCRQ_LEVELS:
        min_criticality = DEFAULT_MIN_CRITICALITY
    by_id = {str(a.get("id")): a for a in assets if a.get("id")}
    by_name: dict[str, list[dict]] = {}
    for asset in by_id.values():
        name = _asset_name(asset).casefold()
        if name:
            by_name.setdefault(name, []).append(asset)

    updated = profile.model_copy(deep=True)
    rated = [{"criticality": primary_asset_level(a)} for a in by_id.values()]
    plan = SyncPlan(profile=updated, min_criticality=min_criticality, by_criticality=count_by_criticality(rated))
    jewels: list[PrimaryAsset] = []
    claimed: set[str] = set()
    for jewel in updated.primary_assets:
        asset_id = jewel.opencrq_asset_id
        if asset_id and asset_id not in by_id and jewel.source == "opencrq":
            # A row an older build keyed on a SUPPORTING asset: the import supersedes it.
            jewels.append(jewel)
            continue
        if not asset_id:
            matches = by_name.get(jewel.name.casefold(), [])
            if len(matches) > 1:
                plan.ambiguous.append(jewel.name)
            elif matches:
                asset_id = str(matches[0]["id"])
                jewel = jewel.model_copy(update={"opencrq_asset_id": asset_id})
                plan.linked.append((jewel.name, asset_id))
        if asset_id:
            if asset_id in by_id:
                claimed.add(asset_id)
            else:
                plan.stale.append((jewel.name, asset_id))
        jewels.append(jewel)

    named = {j.name.casefold() for j in jewels}
    for asset_id, asset in by_id.items():
        if asset_id in claimed:
            continue
        pulled = opencrq_primary_asset(asset, min_criticality)
        if pulled is None or pulled.name.casefold() in named:
            continue
        jewels.append(pulled)
        named.add(pulled.name.casefold())
        plan.pulled.append((asset_id, pulled.name))

    updated.primary_assets = jewels[:200]
    return plan


async def _opencrq_connection(db: AsyncSession, user_id: str) -> tuple[str, Any] | str:
    """``(base_url, headers)`` of the company-managed OpenCRQ for *user_id*, or why not."""
    from sqlalchemy import select

    from app.api.v1.helpers import integration_visible_filter
    from app.integrations.providers.opencrq._common import _headers
    from app.models.integration import Integration
    from app.services.integration_tools import _get_token

    integration = (
        (
            await db.execute(
                select(Integration)
                .where(
                    Integration.provider == "opencrq",
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
        return "no company-managed OpenCRQ integration is registered"
    from app.services.module_identity import platform_is_registered, service_account_refusal

    registered = await platform_is_registered(db, "opencrq")
    if (refusal := service_account_refusal("opencrq", integration, user_id, registered=registered)) is not None:
        return refusal
    token = await _get_token(db, str(integration.id), "opencrq", user_id=user_id)
    if not token or "||" not in token:
        return "no OpenCRQ token could be minted for this administrator"
    base_url, _, api_token = token.partition("||")
    return base_url.strip().rstrip("/"), _headers(api_token.strip())


async def _list_primary_assets(base_url: str, headers: Any, tenant: str) -> tuple[list[dict[str, Any]], str | None]:
    """The tenant's OpenCRQ PRIMARY assets, every organization of it (#1591), at most :data:`MAX_ASSETS`."""
    from app.integrations.providers.opencrq._common import _trpc

    organizations, err = await _trpc(
        base_url, headers, "organizations.getOrganizations", "list organizations", tenant_id=tenant
    )
    if err:
        return [], str(err)
    assets: list[dict[str, Any]] = []
    for organization in organizations or []:
        organization_id = str((organization or {}).get("id") or "")
        if not organization_id:
            continue
        items, err = await _trpc(
            base_url,
            headers,
            "primaryAssets.getPrimaryAssets",
            "list primary assets",
            input={"organizationId": organization_id},
            tenant_id=tenant,
        )
        if err:
            return assets, str(err)
        assets.extend(a for a in (items or []) if isinstance(a, dict))
        if len(assets) >= MAX_ASSETS:
            break
    return assets[:MAX_ASSETS], None


async def _list_assets(base_url: str, headers: Any, tenant: str) -> tuple[list[dict], str | None]:
    from app.integrations.providers.opencrq._common import _trpc

    assets: list[dict] = []
    page = 1
    while len(assets) < MAX_ASSETS:
        result, err = await _trpc(
            base_url, headers, "supportingAssets.listSupportingAssets", "list supporting assets",
            input={"page": page, "limit": 500, "sortDesc": False}, tenant_id=tenant,
        )
        if err:
            return assets, str(err)
        items = (result or {}).get("items") or []
        assets.extend(items)
        if len(items) < 500:
            break
        page += 1
    return assets[:MAX_ASSETS], None


async def saved_min_criticality(db: AsyncSession) -> str:
    """The minimum security need of the saved OpenCRQ primary-asset sync, else every primary asset (#1591)."""
    from sqlalchemy import select

    from app.models.organization_context import OrganizationContextSync

    row = (
        (
            await db.execute(
                select(OrganizationContextSync)
                .where(OrganizationContextSync.section == "primary_assets", OrganizationContextSync.source == "opencrq")
                .order_by(OrganizationContextSync.created_at)
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    level = str(((row.filters or {}) if row is not None else {}).get("min_criticality") or "")
    return level or DEFAULT_MIN_CRITICALITY


async def run_sync(
    db: AsyncSession, *, user_id: uuid.UUID, apply: bool, tenant_id: str = "", min_criticality: str = ""
) -> tuple[SyncPlan | None, list[str]]:
    """Plan the sync, and with *apply* write both sides; ``(plan, errors)``.

    *min_criticality* empty reads the saved OpenCRQ primary-asset sync's filter, so the
    reconcile and the sync pull the same assets; an administrator may pick another
    level on the check (#1558), an unknown one falls back to the default.
    """
    from app.integrations.providers.opencrq._common import _resolve_tenant
    from app.services import organization_context as org_context

    connection = await _opencrq_connection(db, str(user_id))
    if isinstance(connection, str):
        return None, [connection]
    base_url, headers = connection
    tenant, err = await _resolve_tenant(base_url, headers, tenant_id)
    if err:
        return None, [str(err)]
    assets, err = await _list_primary_assets(base_url, headers, tenant or "")
    if err:
        return None, [err]
    current = await org_context.current_version(db)
    level = min_criticality or await saved_min_criticality(db)
    plan = plan_sync(org_context.parse_profile(current.profile if current else {}), assets, level)
    if apply and plan.profile_changed:
        # #1591: nothing is written to OpenCRQ; its primary assets' security needs are its own.
        await org_context.save_version(db, plan.profile, source="api", author_user_id=user_id)
    return plan, []
