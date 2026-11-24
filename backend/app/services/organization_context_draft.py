# SPDX-License-Identifier: Apache-2.0

"""Draft the organization profile from what the connected platforms hold (#8987, slice 2b).

The onboarding step of ``dev-docs/ctem-fleets.md`` section 6: before an
administrator types the profile, XTM One proposes what OpenCTI, OpenAEV
and OpenCRQ already know. The draft is a proposal, never a save: the
administrator reviews it in Settings > Organization and saves it as the
next version with source ``draft``.

What each platform gives, read with a token minted for that administrator:

- **OpenCTI**: the platform organization's name and first sector
  (``settings.platform_organization``), and the PIR ids (``pirs``);
- **OpenAEV**: the security platforms (EDR, SIEM, ...) as software entries;
- **OpenCRQ**: the primary assets (business assets) as crown jewels linked to
  their primary asset, criticality the highest of their security needs
  (``very_high`` -> ``critical``, none rated -> ``high``; #1591). Supporting
  assets are the IT under them, never crown jewels.

Rules: only an EMPTY field of the current profile is filled (what a person
typed always wins); every filled field names the platform it came from; a
platform that is not connected or cannot be read is reported, never
guessed, and never fails the draft. No model is involved: the draft is the
platforms' data, mapped.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.utils import format_exception_message
from app.services.organization_context import CrownJewel, OrganizationProfile, SoftwareEntry

logger = logging.getLogger(__name__)

#: OpenCRQ supporting-asset criticality -> profile crown-jewel criticality.
#: Lower ratings are not crown jewels and are left out of the draft.
FROM_OPENCRQ: dict[str, str] = {"very_high": "critical", "high": "high"}
#: Entries read at most per platform list.
MAX_ITEMS = 100

_OCTI_DRAFT = """
query XtmOrganizationDraft {
  settings { platform_organization { name sectors { edges { node { name } } } } }
}
"""
_OCTI_PIRS = """
query XtmOrganizationDraftPirs($first: Int, $after: ID) {
  pirs(first: $first, after: $after) {
    edges { node { id name } }
    pageInfo { hasNextPage endCursor }
  }
}
"""
#: Pages of :data:`MAX_ITEMS` PIRs read at most (#11589): 5,000 PIRs, far past any real OpenCTI.
MAX_PIR_PAGES = 50


@dataclass
class PlatformFacts:
    """What the connected platforms hold, already mapped to profile fields."""

    name: str = ""
    sector: str = ""
    pir_ids: list[str] = field(default_factory=list)
    #: OpenCTI's name of each PIR in ``pir_ids`` (#11589): a drafted PIR is saved with its name.
    pir_names: dict[str, str] = field(default_factory=dict)
    software: list[SoftwareEntry] = field(default_factory=list)
    crown_jewels: list[CrownJewel] = field(default_factory=list)
    #: profile field -> the platform the value came from.
    sources: dict[str, str] = field(default_factory=dict)
    #: one line per platform that could not be read, and why.
    unavailable: list[str] = field(default_factory=list)


@dataclass
class Draft:
    profile: OrganizationProfile
    filled: list[dict[str, str]]
    unavailable: list[str]


def merge_draft(current: OrganizationProfile, facts: PlatformFacts) -> Draft:
    """Fill the empty fields of *current* from *facts* (no I/O; *current* is untouched)."""
    draft = current.model_copy(deep=True)
    filled: list[dict[str, str]] = []

    def _fill(path: str, value: Any, detail: str) -> None:
        if not value or getattr(draft, path):
            return
        setattr(draft, path, value)
        filled.append({"field": path, "source": facts.sources.get(path, ""), "detail": detail})

    _fill("name", facts.name[:200], facts.name[:200])
    _fill("sector", facts.sector[:100], facts.sector[:100])
    _fill("pir_ids", facts.pir_ids[:100], f"{len(facts.pir_ids[:100])} PIR(s)")
    # Every PIR the draft holds keeps its OpenCTI name, a name already saved included (#11589).
    draft.pir_names = {**{k: v for k, v in facts.pir_names.items() if k in draft.pir_ids}, **draft.pir_names}
    _fill("software", facts.software[:500], f"{len(facts.software[:500])} product(s)")
    _fill("crown_jewels", facts.crown_jewels[:200], f"{len(facts.crown_jewels[:200])} asset(s)")
    return Draft(profile=draft, filled=filled, unavailable=list(facts.unavailable))


def opencti_facts(settings_data: dict, pirs_data: dict, facts: PlatformFacts) -> None:
    """Map OpenCTI's answers onto *facts* (no I/O)."""
    org = ((settings_data or {}).get("settings") or {}).get("platform_organization") or {}
    name = str(org.get("name") or "").strip()
    if name:
        facts.name = name
        facts.sources["name"] = "opencti"
    sectors = [
        str((edge.get("node") or {}).get("name") or "").strip()
        for edge in ((org.get("sectors") or {}).get("edges") or [])
    ]
    sectors = [s for s in sectors if s]
    if sectors:
        facts.sector = sectors[0]
        facts.sources["sector"] = "opencti"
    rows = opencti_pirs(pirs_data)
    if rows:
        facts.pir_ids = [row["id"] for row in rows]
        facts.pir_names = {row["id"]: row["name"] for row in rows if row["name"] != row["id"]}
        facts.sources["pir_ids"] = "opencti"


def openaev_facts(platforms: list[dict], facts: PlatformFacts) -> None:
    """Map OpenAEV's security platforms onto the software inventory of *facts* (no I/O)."""
    seen: set[str] = set()
    entries: list[SoftwareEntry] = []
    for item in platforms[:MAX_ITEMS]:
        product = str(item.get("asset_name") or item.get("security_platform_name") or "").strip()
        if not product or product.casefold() in seen:
            continue
        seen.add(product.casefold())
        entries.append(SoftwareEntry(product=product[:200]))
    if entries:
        facts.software = entries
        facts.sources["software"] = "openaev"


def opencrq_facts(assets: list[dict], facts: PlatformFacts) -> None:
    """Map OpenCRQ's PRIMARY assets onto crown jewels (no I/O, #1591)."""
    from app.services.organization_context_import import opencrq_primary_asset

    seen: set[str] = set()
    jewels: list[CrownJewel] = []
    for asset in assets:
        row = opencrq_primary_asset(asset)
        if row is None or row.name.casefold() in seen:
            continue
        seen.add(row.name.casefold())
        jewels.append(
            CrownJewel(
                name=row.name,
                business_service=row.business_service,
                owner=row.owner,
                criticality=row.criticality,
                opencrq_asset_id=row.external_id[:100],
            )
        )
    # The most critical first, so a cut at 200 keeps them.
    jewels.sort(key=lambda j: j.criticality != "critical")
    if jewels:
        facts.crown_jewels = jewels
        facts.sources["crown_jewels"] = "opencrq"


async def _read_opencti(db: AsyncSession, user_id: str, facts: PlatformFacts) -> None:
    from app.integrations.providers.opencti._common import _client, _graphql
    from app.services.ctem_metrics_collector import _user_creds

    creds = await _user_creds(db, user_id, "opencti")
    if creds is None:
        facts.unavailable.append("OpenCTI: not connected, or no token for this administrator")
        return
    base_url, headers = creds
    async with _client(headers, timeout=30) as client:
        settings_data, err = await _graphql(client, base_url, headers, _OCTI_DRAFT)
        if err:
            facts.unavailable.append(f"OpenCTI: {err}")
            return
        pirs_data, pir_err = await read_all_pirs(client, base_url, headers)
    if pir_err:
        # PIRs are Enterprise Edition: their absence never hides the rest.
        facts.unavailable.append(f"OpenCTI PIRs: {pir_err}")
        pirs_data = {}
    opencti_facts(settings_data, pirs_data, facts)


async def read_all_pirs(client: Any, base_url: str, headers: dict) -> tuple[dict, str | None]:
    """Every PIR of OpenCTI, page after page (#11589: the first 100 only left the rest unnamed).

    Answers ``({"pirs": {"edges": [...]}}, None)``, or ``({}, error)`` when a page fails.
    """
    from app.integrations.providers.opencti._common import _graphql

    edges: list[dict] = []
    after: str | None = None
    for _ in range(MAX_PIR_PAGES):
        data, err = await _graphql(client, base_url, headers, _OCTI_PIRS, {"first": MAX_ITEMS, "after": after})
        if err:
            return {}, err
        page = (data or {}).get("pirs") or {}
        edges.extend(page.get("edges") or [])
        info = page.get("pageInfo") or {}
        after = info.get("endCursor")
        if not info.get("hasNextPage") or not after:
            break
    return {"pirs": {"edges": edges}}, None


def opencti_pirs(pirs_data: dict) -> list[dict[str, str]]:
    """OpenCTI's PIRs as ``{id, name}`` rows, deduplicated, the name falling back to the id (no I/O)."""
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for edge in ((pirs_data or {}).get("pirs") or {}).get("edges") or []:
        node = edge.get("node") or {}
        pir_id = str(node.get("id") or "").strip()
        if not pir_id or pir_id in seen:
            continue
        seen.add(pir_id)
        rows.append({"id": pir_id, "name": str(node.get("name") or "").strip() or pir_id})
    return rows


async def list_pirs(db: AsyncSession, *, user_id: uuid.UUID) -> tuple[list[dict[str, str]], str | None, str]:
    """The PIRs of the connected OpenCTI for *user_id*, the reason they cannot be read, and OpenCTI's base URL.

    The base URL (empty when not connected) lets the page link each PIR to
    OpenCTI, also when the list itself could not be read (#9615).
    """
    from app.integrations.providers.opencti._common import _client
    from app.services.ctem_metrics_collector import _user_creds

    creds = await _user_creds(db, str(user_id), "opencti")
    if creds is None:
        return [], "not_connected", ""
    base_url, headers = creds
    link_base = str(base_url).rstrip("/")
    try:
        async with _client(headers, timeout=30) as client:
            data, err = await read_all_pirs(client, base_url, headers)
    except Exception as exc:  # the picker falls back to the saved names, never fails the page
        logger.warning("Organization PIR list: OpenCTI unreadable: %s", format_exception_message(exc))
        return [], "unreadable", link_base
    if err:
        # PIRs are Enterprise Edition: an OpenCTI without them answers an error here.
        return [], "unreadable", link_base
    return opencti_pirs(data), None, link_base


def missing_pir_names(ids: list[str], names: dict[str, str], rows: list[dict[str, str]]) -> dict[str, str]:
    """OpenCTI's name of each PIR of *ids* that has none in *names* (no I/O, #11589).

    A name already saved is never overwritten, and a PIR OpenCTI no longer
    returns keeps its last known name: only the gaps are filled.
    """
    live = {row["id"]: row["name"][:300] for row in rows if row.get("name") and row["name"] != row["id"]}
    return {pir: live[pir] for pir in ids if not names.get(pir) and pir in live}


async def name_saved_pirs(db: AsyncSession, *, user_id: uuid.UUID, profile: OrganizationProfile) -> OrganizationProfile:
    """*profile* with an OpenCTI name for each PIR saved without one (#11589).

    Every write path (the API, a chat proposal, the onboarding tool) calls it
    before saving, so a PIR is stored with its label whoever wrote its id.
    No unnamed PIR: no OpenCTI read. OpenCTI unreadable: *profile* unchanged.
    """
    names = dict(profile.pir_names)
    if all(names.get(pir) for pir in profile.pir_ids):
        return profile
    try:
        rows, _unavailable, _url = await list_pirs(db, user_id=user_id)
    except Exception as exc:  # a name lookup never refuses a save: the page names it later
        logger.warning("Organization PIR names: OpenCTI lookup failed: %s", format_exception_message(exc))
        return profile
    found = missing_pir_names(profile.pir_ids, names, rows)
    return profile.model_copy(update={"pir_names": {**names, **found}}) if found else profile


async def backfill_pir_names(db: AsyncSession, rows: list[dict[str, str]]) -> int:
    """Store the names OpenCTI just gave for the saved PIRs that had none, in place (#11589).

    The lazy backfill of the profiles saved before every write path stored
    names: the current version gains the missing names, its ``pir_ids`` and
    every other field untouched, no new version (``pir_names`` is not a
    contract field). Answers how many names were stored.
    """
    from app.services import organization_context as org_context

    row = await org_context.current_version(db)
    if row is None:
        return 0
    stored = dict(row.profile or {})
    ids = [str(pir) for pir in stored.get("pir_ids") or []]
    names = {str(k): str(v) for k, v in (stored.get("pir_names") or {}).items()}
    found = missing_pir_names(ids, names, rows)
    if not found:
        return 0
    try:
        row.profile = {**stored, "pir_names": {**names, **found}}
        await db.commit()
    except Exception as exc:  # the backfill never fails the PIR list it rides on
        await db.rollback()
        logger.warning("Organization PIR names: backfill not stored: %s", format_exception_message(exc))
        return 0
    return len(found)


async def _read_openaev(db: AsyncSession, user_id: str, facts: PlatformFacts) -> None:
    from app.integrations.providers.openaev._common import _api, _search_body
    from app.services.ctem_metrics_collector import _user_creds

    creds = await _user_creds(db, user_id, "openaev")
    if creds is None:
        facts.unavailable.append("OpenAEV: not connected, or no token for this administrator")
        return
    base_url, headers = creds
    resp, err = await _api(
        base_url, headers, "post", "/api/security_platforms/search", "search security platforms",
        json=_search_body(size=MAX_ITEMS), retry_transient=True,
    )
    if err or resp is None:
        facts.unavailable.append(f"OpenAEV: {err or 'no answer'}")
        return
    data = resp.json()
    items = data.get("content", []) if isinstance(data, dict) else data
    openaev_facts([i for i in (items or []) if isinstance(i, dict)], facts)


async def _read_opencrq(db: AsyncSession, user_id: str, tenant_id: str, facts: PlatformFacts) -> None:
    from app.integrations.providers.opencrq._common import _resolve_tenant
    from app.services.organization_context_opencrq import _list_primary_assets, _opencrq_connection

    connection = await _opencrq_connection(db, user_id)
    if isinstance(connection, str):
        facts.unavailable.append(f"OpenCRQ: {connection}")
        return
    base_url, headers = connection
    tenant, err = await _resolve_tenant(base_url, headers, tenant_id)
    if err:
        facts.unavailable.append(f"OpenCRQ: {err}")
        return
    assets, err = await _list_primary_assets(base_url, headers, tenant or "")
    if err:
        facts.unavailable.append(f"OpenCRQ: {err}")
        return
    opencrq_facts(assets, facts)


async def collect_facts(db: AsyncSession, *, user_id: uuid.UUID, tenant_id: str = "") -> PlatformFacts:
    """Read the three platforms for *user_id*; a platform that fails is reported, never raised."""
    facts = PlatformFacts()
    readers = (
        ("OpenCRQ", lambda: _read_opencrq(db, str(user_id), tenant_id, facts)),
        ("OpenCTI", lambda: _read_opencti(db, str(user_id), facts)),
        ("OpenAEV", lambda: _read_openaev(db, str(user_id), facts)),
    )
    # One after the other: they share the request's database session.
    for label, read in readers:
        try:
            await read()
        except Exception as exc:  # one platform never breaks the draft
            logger.warning("Organization draft: %s unreadable: %s", label, format_exception_message(exc))
            facts.unavailable.append(f"{label}: {type(exc).__name__}")
    return facts


async def draft_profile(db: AsyncSession, *, user_id: uuid.UUID, tenant_id: str = "") -> tuple[Draft, int | None]:
    """The draft over the current profile, and the version it was drafted from (writes nothing)."""
    from app.services import organization_context as org_context

    # The asset lists come from the records (#1543): a draft saved over the stored JSON would drop the row writes.
    current, base = await org_context.current_profile(db)
    version = current.version if current else None
    facts = await collect_facts(db, user_id=user_id, tenant_id=tenant_id)
    return merge_draft(base, facts), version
