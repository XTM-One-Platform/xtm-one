# SPDX-License-Identifier: Apache-2.0

"""OpenCRQ crown-jewel sync of the organization profile (#8987, slice 3d).

The profile's crown jewels (``services/organization_context.py``) and
OpenCRQ's supporting assets describe the same assets twice. An administrator
runs the sync on demand (``POST /api/v1/organization-context/opencrq-sync``,
a dry run unless ``apply`` is set) and it reconciles them both ways:

- **link**: a crown jewel without ``opencrq_asset_id`` whose name matches one
  supporting asset (case-insensitive, exactly one) gets that asset's id;
- **profile -> OpenCRQ**: a linked jewel whose criticality differs from the
  asset's sets the asset's criticality (the profile is where the business
  says what matters);
- **OpenCRQ -> profile**: a supporting asset rated ``very_high`` that no
  jewel names joins the profile as a ``critical`` crown jewel linked to it;
- a jewel whose ``opencrq_asset_id`` OpenCRQ no longer holds is reported,
  never removed.

:func:`plan_sync` is the pure rule; :func:`run_sync` reads OpenCRQ through
the company-managed integration with a token minted for the administrator,
saves the next profile version (source ``api``) and writes the criticalities
only when ``apply`` is set. Nothing is guessed: an ambiguous name is reported.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.organization_context import CrownJewel, OrganizationProfile

#: Profile criticality -> OpenCRQ supporting-asset criticality.
TO_OPENCRQ: dict[str, str] = {"low": "low", "medium": "medium", "high": "high", "critical": "very_high"}
#: The OpenCRQ rating that makes an unnamed asset a crown jewel of the profile.
PULLED_CRITICALITY = "very_high"
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

    @property
    def profile_changed(self) -> bool:
        return bool(self.linked or self.pulled)

    def summary(self) -> dict[str, Any]:
        return {
            "linked": [{"crown_jewel": j, "opencrq_asset_id": a} for j, a in self.linked],
            "pushed_to_opencrq": [{"opencrq_asset_id": a, "name": n, "criticality": c} for a, n, c in self.pushed],
            "pulled_from_opencrq": [{"opencrq_asset_id": a, "name": n} for a, n in self.pulled],
            "ambiguous": list(self.ambiguous),
            "not_in_opencrq": [{"crown_jewel": j, "opencrq_asset_id": a} for j, a in self.stale],
        }


def _asset_name(asset: dict) -> str:
    return str(asset.get("name") or asset.get("derivedName") or "").strip()


def plan_sync(profile: OrganizationProfile, assets: list[dict]) -> SyncPlan:
    """Reconcile *profile*'s crown jewels with OpenCRQ's supporting *assets* (no I/O)."""
    by_id = {str(a.get("id")): a for a in assets if a.get("id")}
    by_name: dict[str, list[dict]] = {}
    for asset in by_id.values():
        name = _asset_name(asset).casefold()
        if name:
            by_name.setdefault(name, []).append(asset)

    updated = profile.model_copy(deep=True)
    plan = SyncPlan(profile=updated)
    jewels: list[CrownJewel] = []
    claimed: set[str] = set()
    for jewel in updated.crown_jewels:
        asset_id = jewel.opencrq_asset_id
        if not asset_id:
            matches = by_name.get(jewel.name.casefold(), [])
            if len(matches) > 1:
                plan.ambiguous.append(jewel.name)
            elif matches:
                asset_id = str(matches[0]["id"])
                jewel = jewel.model_copy(update={"opencrq_asset_id": asset_id})
                plan.linked.append((jewel.name, asset_id))
        if asset_id:
            asset = by_id.get(asset_id)
            if asset is None:
                plan.stale.append((jewel.name, asset_id))
            else:
                claimed.add(asset_id)
                wanted = TO_OPENCRQ[jewel.criticality]
                if str(asset.get("criticality") or "").lower() != wanted:
                    plan.pushed.append((asset_id, _asset_name(asset), wanted))
        jewels.append(jewel)

    named = {j.name.casefold() for j in jewels}
    for asset_id, asset in by_id.items():
        name = _asset_name(asset)
        if (
            asset_id in claimed
            or str(asset.get("criticality") or "").lower() != PULLED_CRITICALITY
            or not name
            or name.casefold() in named
        ):
            continue
        jewels.append(CrownJewel(name=name[:200], criticality="critical", opencrq_asset_id=asset_id[:100]))
        named.add(name.casefold())
        plan.pulled.append((asset_id, name))

    updated.crown_jewels = jewels[:200]
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


async def run_sync(
    db: AsyncSession, *, user_id: uuid.UUID, apply: bool, tenant_id: str = ""
) -> tuple[SyncPlan | None, list[str]]:
    """Plan the sync, and with *apply* write both sides; ``(plan, errors)``."""
    from app.integrations.providers.opencrq._common import _resolve_tenant, _trpc
    from app.services import organization_context as org_context

    connection = await _opencrq_connection(db, str(user_id))
    if isinstance(connection, str):
        return None, [connection]
    base_url, headers = connection
    tenant, err = await _resolve_tenant(base_url, headers, tenant_id)
    if err:
        return None, [str(err)]
    assets, err = await _list_assets(base_url, headers, tenant or "")
    if err:
        return None, [err]
    current = await org_context.current_version(db)
    plan = plan_sync(org_context.parse_profile(current.profile if current else {}), assets)
    if not apply:
        return plan, []

    errors: list[str] = []
    for asset_id, name, criticality in plan.pushed:
        _, err = await _trpc(
            base_url, headers, "supportingAssets.updateSupportingAsset", "update supporting asset",
            input={"id": asset_id, "criticality": criticality}, mutation=True, tenant_id=tenant or "",
        )
        if err:
            errors.append(f"{name or asset_id}: {err}")
    if plan.profile_changed:
        await org_context.save_version(db, plan.profile, source="api", author_user_id=user_id)
    return plan, errors
