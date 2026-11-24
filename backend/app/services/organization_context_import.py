# SPDX-License-Identifier: Apache-2.0

"""Import one section of the organization profile from one connected platform (#11780).

The no-model path of Settings > Organization: the administrator picks a
section (assets, asset groups, AI systems, tech stack, crown jewels, PIRs),
a source platform able to feed it (OpenAEV, OpenCTI, OpenCRQ), the filters
that source supports, and gets a preview before anything is written: the
source's own count of matching entities, then what the profile would gain
(new, updated, unchanged, skipped) with a sample of rows. Applying saves the
next version.

Rules:

- read with the administrator's own token (``_user_creds``), never another;
- a platform not connected or not readable is reported with the reason,
  never guessed, and never assumed installed;
- rows a person typed (``source`` ``manual``, ``csv`` or ``api`` on an asset,
  no platform id on an AI system or a crown jewel) are never overwritten: an
  imported row with the same name is skipped and counted;
- an imported row carries ``source=<platform>`` and ``external_id``; a re-run
  with the same source updates its own rows only (matched by both);
- no model call.

Assets are typed on OpenAEV's category / subcategory taxonomy (#11781):
an OpenAEV asset keeps its own category, subcategory and criticality, and a
batch of OpenCTI Systems takes the category (or category/subcategory) picked
for the batch (:func:`batch_taxonomy`). Asset groups go to ``asset_groups``.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.utils import format_exception_message
from app.services.organization_context import (
    ASSET_CRITICALITIES,
    ASSET_ENVIRONMENTS,
    ASSET_SUBCATEGORIES,
    ASSET_TAXONOMY,
    INTERNET_FACING,
    LEGACY_ASSET_TYPES,
    AssetEntry,
    AssetGroupEntry,
    CrownJewel,
    OrganizationProfile,
    TechStackEntry,
)
from app.services.organization_context_microsoft import os_family

logger = logging.getLogger(__name__)

Section = Literal["assets", "asset_groups", "ai_systems", "tech_stack", "crown_jewels", "pirs"]
Source = Literal[
    "openaev", "opencti", "opencrq", "intune", "defender", "tanium", "crowdstrike", "servicenow", "github", "gitlab"
]

#: section -> the platforms that can feed it, in the order the dialog lists them.
SECTION_SOURCES: dict[str, tuple[str, ...]] = {
    "assets": ("openaev", "opencti", "intune", "defender", "tanium", "crowdstrike", "servicenow", "github", "gitlab"),
    # #1556: Intune (Entra device groups), Tanium computer groups, Falcon host groups, ServiceNow services.
    "asset_groups": ("openaev", "intune", "tanium", "crowdstrike", "servicenow"),
    "ai_systems": ("openaev",),
    "tech_stack": ("openaev",),
    "crown_jewels": ("opencrq",),
    "pirs": ("opencti",),
}
#: Deprecated since #1501 (kept one release): ``ai_systems`` reads and writes the assets of category
#: ``ai_target``; an import of it touches no other asset.
AI_SYSTEMS_ALIAS = "ai_systems"
#: section -> the profile field it writes.
SECTION_FIELD: dict[str, str] = {
    "assets": "assets_exposure",
    "asset_groups": "asset_groups",
    "ai_systems": "assets_exposure",
    "tech_stack": "tech_stack",
    "crown_jewels": "crown_jewels",
    "pirs": "pir_ids",
}
#: (section, source) -> the filters that source supports for that section, in the order the dialog shows them.
SOURCE_FILTERS: dict[tuple[str, str], tuple[str, ...]] = {
    ("assets", "openaev"): ("asset_groups", "tags", "category", "subcategory", "criticality", "platform"),
    ("assets", "opencti"): (
        "labels",
        "markings",
        "created_by",
        "creator",
        "category",
        "subcategory",
        "criticality",
        "internet_facing",
        "environment",
        "platform",
        "created_after",
        "filter_group",
    ),
    # Microsoft endpoint sources (#1251): devices, OS and software with versions.
    ("assets", "intune"): ("operating_system", "compliance", "ownership", "last_seen_days", "include_owner"),
    ("assets", "defender"): ("os_platform", "health_status", "exposure_level", "last_seen_days"),
    # Endpoint and CMDB sources (#1556): devices with their identity keys, Tanium software with versions.
    ("assets", "tanium"): ("operating_system", "last_seen_days", "skip_software"),
    ("assets", "crowdstrike"): ("platform", "last_seen_days", "filter_query"),
    ("assets", "servicenow"): ("ci_class", "operational_status", "query"),
    # Code sources: repositories as source_code assets, their SBOM as software.
    ("assets", "github"): ("organizations", "include_archived", "include_forks", "skip_dependencies"),
    ("assets", "gitlab"): ("groups", "include_archived", "include_forks", "skip_dependencies"),
    ("asset_groups", "openaev"): ("tags", "search"),
    ("asset_groups", "intune"): ("search",),
    ("asset_groups", "tanium"): ("search",),
    ("asset_groups", "crowdstrike"): ("search",),
    ("asset_groups", "servicenow"): ("search",),
    ("ai_systems", "openaev"): ("tags", "search"),
    ("tech_stack", "openaev"): ("type",),
    ("crown_jewels", "opencrq"): ("min_criticality", "tenant_id"),
    ("pirs", "opencti"): ("pir_ids",),
}
#: The OpenCTI System attributes of OpenCTI-Platform/opencti#19943 (the OpenAEV asset taxonomy on a System):
#: filter key -> attribute. An OpenCTI without them is read with the base query and these filters are hidden.
OPENCTI_SYSTEM_ATTRIBUTES: dict[str, str] = {
    "category": "x_opencti_system_category",
    "subcategory": "x_opencti_system_subcategory",
    "criticality": "x_opencti_criticality",
    "internet_facing": "x_opencti_internet_facing",
    "environment": "x_opencti_environment",
    "platform": "x_opencti_platform",
}
#: filter key -> the OpenCTI vocabulary category listing its values (#19943).
OPENCTI_SYSTEM_VOCABULARIES: dict[str, str] = {
    "category": "system_category_ov",
    "subcategory": "system_subcategory_ov",
    "criticality": "system_criticality_ov",
    "internet_facing": "system_internet_facing_ov",
    "environment": "system_environment_ov",
    "platform": "system_platform_ov",
}
#: OpenAEV endpoint platforms (the values of ``endpoint_platform``).
OPENAEV_PLATFORMS: tuple[str, ...] = (
    "Linux",
    "Windows",
    "MacOS",
    "iOS",
    "Android",
    "Container",
    "Service",
    "Generic",
    "Internal",
    "Unknown",
)
#: Entities read at most per import (the source's own count is still reported).
MAX_ROWS = 1000
#: Rows of the preview sample.
SAMPLE_SIZE = 20
#: OpenCRQ criticality, lowest first; a threshold keeps it and everything above.
OPENCRQ_LEVELS: tuple[str, ...] = ("low", "medium", "high", "very_high")
#: OpenCRQ criticality -> crown jewel criticality.
FROM_OPENCRQ: dict[str, str] = {"very_high": "critical", "high": "high", "medium": "medium", "low": "low"}
#: OpenAEV asset criticality -> profile criticality.
FROM_OPENAEV_CRITICALITY: dict[str, str] = {
    "critical": "critical",
    "very_high": "critical",
    "high": "high",
    "medium": "medium",
    "low": "low",
}


@dataclass
class SourceStatus:
    """Whether a platform can feed a section for this administrator, and why not."""

    source: str
    available: bool
    reason: str = ""
    #: The reason as a code the dialog words in the user's language (empty: word ``reason`` as given).
    reason_code: str = ""


@dataclass
class ReadResult:
    """What a source answered: the mapped rows, its own count, the error that stopped it."""

    rows: list[Any] = field(default_factory=list)
    #: The source's own count of matching entities (``totalElements``, ``globalCount``); ``None`` unknown.
    total: int | None = None
    error: str | None = None
    #: True when the source holds more matching entities than :data:`MAX_ROWS`.
    truncated: bool = False
    #: Rows that took the batch type because the source gave them none (OpenCTI Systems without a category).
    batch_typed: int = 0
    #: Software per row ``external_id`` an endpoint source read (#1251), stored on the device record at save.
    software: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    #: Software entries read (all devices).
    software_rows: int = 0
    #: Devices whose software read failed: their stored software is kept (never read as "none").
    software_unread: set[str] = field(default_factory=set)
    #: Why the software read failed (devices still sync): said in the run status, never a silent zero.
    software_error: str = ""
    #: GitLab dependency vulnerabilities per project read whole (#1331), recorded as CTEM signals at save.
    findings: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    #: Name and identity key per repository id (#1331, slice 4): the asset a dependency match lands on.
    repositories: dict[str, dict[str, str]] = field(default_factory=dict)
    #: An asset group source (#1556): member asset ids per group ``external_id``, each group read whole.
    members: dict[str, list[str]] = field(default_factory=dict)
    #: The source whose asset records those member ids name.
    members_source: str = ""


@dataclass
class ImportPlan:
    """The preview of an import: counts, the sample, and the profile it would save."""

    section: str
    source: str
    source_total: int | None
    read: int
    new: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    #: Rows a previous import from this source wrote that this read no longer returns (left the source or the filters).
    gone: list[str] = field(default_factory=list)
    #: True when the administrator chose to remove the ``gone`` rows (default: they are kept).
    remove_gone: bool = False
    #: ``gone`` rows a sync kept because another writer wrote them (#1473): only its own rows go.
    gone_kept: list[str] = field(default_factory=list)
    #: Rows the source returned that the sync excludes (its ``excluded_external_ids``, #1134).
    excluded: list[str] = field(default_factory=list)
    #: Stored rows of an excluded id, removed by this plan whatever ``remove_gone`` says.
    excluded_removed: list[str] = field(default_factory=list)
    #: The sync writing the plan, stamped as ``sync_id`` on its rows (empty: a one-off import).
    sync_id: str = ""
    #: Rows that took the batch type (no category in the source).
    batch_typed: int = 0
    sample: list[dict[str, Any]] = field(default_factory=list)
    truncated: bool = False
    error: str | None = None
    profile: OrganizationProfile | None = None
    #: Software per device an endpoint source read (#1251); written by :func:`store_endpoint_software`.
    software: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    software_rows: int = 0
    #: Devices whose software read failed: :func:`store_endpoint_software` keeps their stored software.
    software_unread: set[str] = field(default_factory=set)
    #: Why the software read failed for those devices (empty: every read answered).
    software_error: str = ""
    #: GitLab dependency vulnerabilities per project; recorded by :func:`store_endpoint_software`.
    findings: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    #: Name and identity key per repository id; dependencies matched to OpenCTI after the save.
    repositories: dict[str, dict[str, str]] = field(default_factory=dict)
    #: Assets and asset groups since #1543: the organization whose records the plan was made on. Applied
    #: by :func:`apply_record_import` as row writes (``writes``) and removals (``remove_refs``) by
    #: ``(source, external_id)``, never a profile version; ``profile`` stays ``None``.
    organization_id: uuid.UUID | None = None
    writes: list[dict[str, Any]] = field(default_factory=list)
    remove_refs: list[tuple[str, str]] = field(default_factory=list)
    #: Group memberships the source answered (#1556), written by :func:`apply_record_import`.
    members: dict[str, list[str]] = field(default_factory=dict)
    members_source: str = ""

    @property
    def on_records(self) -> bool:
        return self.organization_id is not None

    @property
    def has_writes(self) -> bool:
        """A record plan that writes or removes at least one row, or re-reads group memberships."""
        return bool(self.writes or self.remove_refs or self.members)

    def as_dict(self) -> dict[str, Any]:
        return {
            "section": self.section,
            "source": self.source,
            "source_total": self.source_total,
            "read": self.read,
            "counts": {
                "new": len(self.new),
                "updated": len(self.updated),
                "unchanged": len(self.unchanged),
                "skipped": len(self.skipped),
                "gone": len(self.gone),
                "excluded": len(self.excluded),
            },
            "new": self.new[:SAMPLE_SIZE],
            "updated": self.updated[:SAMPLE_SIZE],
            "skipped": self.skipped[:SAMPLE_SIZE],
            "gone": self.gone[:SAMPLE_SIZE],
            "gone_kept": self.gone_kept[:SAMPLE_SIZE],
            "excluded": self.excluded[:SAMPLE_SIZE],
            "remove_gone": self.remove_gone,
            "batch_typed": self.batch_typed,
            "software_rows": self.software_rows,
            "software_unread": len(self.software_unread),
            "software_error": self.software_error or None,
            "sample": self.sample,
            "truncated": self.truncated,
            "error": self.error,
        }


# ── Mapping (no I/O) ──────────────────────────────────────────────────


def _yes_no(value: Any) -> str:
    if value is True:
        return "yes"
    if value is False:
        return "no"
    return "unknown"


def batch_taxonomy(batch_type: str) -> tuple[str, str] | None:
    """The (category, subcategory) a batch pick names: ``category`` or ``category/subcategory``.

    A flat type of #11543 (``database``, ``other``...) is still read through
    :data:`LEGACY_ASSET_TYPES`; anything else is ``None``.
    """
    category, _, subcategory = batch_type.strip().lower().partition("/")
    if category in ASSET_TAXONOMY and (not subcategory or ASSET_SUBCATEGORIES.get(subcategory) == category):
        return category, subcategory
    if category in LEGACY_ASSET_TYPES and not subcategory:
        return LEGACY_ASSET_TYPES[category]
    return None


#: OpenAEV ``endpoint_platform`` (lower-cased) -> the (category, subcategory) it stands for when the
#: endpoint names no subcategory of its own: a Windows or macOS endpoint is a workstation a person
#: uses (always reachable, :mod:`app.services.asset_reachability` rule 2), a Linux one a server.
PLATFORM_TAXONOMY: dict[str, tuple[str, str]] = {
    "windows": ("host", "workstation"),
    "macos": ("host", "workstation"),
    "linux": ("host", "server"),
    "ios": ("mobile_device", "smartphone"),
    "android": ("mobile_device", "smartphone"),
    "container": ("container_workload", "container"),
}


def openaev_asset_taxonomy(asset: dict[str, Any], batch_type: str = "") -> tuple[str, str]:
    """The (category, subcategory) of an OpenAEV asset: the batch's pick, else its own, lower-cased.

    An endpoint without a subcategory of its own takes the one its platform
    names (:data:`PLATFORM_TAXONOMY`) when that fits its category, so a
    Windows host is recognised as a user endpoint.
    """
    picked = batch_taxonomy(batch_type) if batch_type else None
    if picked:
        return picked
    category = str(asset.get("asset_category") or "").strip().lower()
    subcategory = str(asset.get("asset_subcategory") or "").strip().lower()
    from_platform = PLATFORM_TAXONOMY.get(str(asset.get("endpoint_platform") or "").strip().lower())
    if category in ASSET_TAXONOMY:
        if ASSET_SUBCATEGORIES.get(subcategory) == category:
            return category, subcategory
        return category, from_platform[1] if from_platform and from_platform[0] == category else ""
    if from_platform:
        return from_platform
    if asset.get("endpoint_platform"):
        return "host", ""
    return "generic_asset", ""


def openaev_asset(asset: dict[str, Any], batch_type: str = "") -> AssetEntry | None:
    """An OpenAEV asset (endpoint) as an ``assets_exposure`` row, or ``None`` without a name or id."""
    name = str(asset.get("asset_name") or "").strip()
    asset_id = str(asset.get("asset_id") or "").strip()
    if not name or not asset_id:
        return None
    category, subcategory = openaev_asset_taxonomy(asset, batch_type)
    criticality = str(asset.get("asset_criticality") or "").strip().lower()
    status = str(asset.get("asset_status") or "").strip().lower()
    return AssetEntry(
        name=name[:200],
        category=category,  # type: ignore[arg-type]
        subcategory=subcategory,
        criticality=criticality if criticality in ASSET_CRITICALITIES else "unknown",  # type: ignore[arg-type]
        internet_facing=_yes_no(asset.get("asset_internet_facing")),  # type: ignore[arg-type]
        source="openaev",
        external_id=asset_id[:200],
        # The hostname the agent reports (``asset_hostname``), an identity key beside the name.
        hostname=str(asset.get("asset_hostname") or "").strip()[:255],
        # Its addresses: the MACs a strong key, the IPs a weak one (possible matches only).
        mac=_openaev_list(asset.get("asset_mac_addresses")),
        ip=_openaev_list(asset.get("asset_ips")),
        # The platform as an OS family named as the Microsoft sources name it (``Windows``, ``macOS``).
        os_family=openaev_os_family(asset.get("endpoint_platform")),
        last_seen=openaev_last_seen(asset.get("asset_last_seen")),
        status=status if status in OPENAEV_ASSET_STATUSES else "",  # type: ignore[arg-type]
        **openaev_ai_target_fields(asset) if category == "ai_target" else {},
    )


#: OpenAEV ``ai_target_modality`` values an asset keeps, lower-cased.
_AI_MODALITIES: frozenset[str] = frozenset({"text", "vision", "audio", "multimodal"})


def openaev_ai_target_fields(asset: dict[str, Any]) -> dict[str, Any]:
    """The ``ai_target_*`` fields of an OpenAEV AI target an asset row keeps (#1501).

    Provider, endpoint, model and modality; the token, the configuration and the system
    prompt are never read (they may carry a credential).
    """
    from app.services.organization_context import AI_TARGET_PROVIDERS

    provider = _lower(asset.get("ai_target_provider"))
    modality = _lower(asset.get("ai_target_modality"))
    return {
        "ai_target_provider": provider if provider in AI_TARGET_PROVIDERS else "",
        "ai_target_endpoint": str(asset.get("ai_target_endpoint") or "").strip()[:500],
        "ai_target_model": str(asset.get("ai_target_model") or "").strip()[:200],
        "ai_target_modality": modality if modality in _AI_MODALITIES else "",
    }


#: OpenAEV ``endpoint_platform`` values that name no operating system.
_OPENAEV_NO_PLATFORM: frozenset[str] = frozenset({"", "unknown", "generic", "internal"})


def openaev_os_family(platform: Any) -> str:
    """OpenAEV's ``endpoint_platform`` as an OS family, the names the Intune and Defender sources use."""
    text = str(platform or "").strip()
    return "" if text.lower() in _OPENAEV_NO_PLATFORM else os_family(text)


def _openaev_list(value: Any) -> list[str]:
    """A list field of an OpenAEV endpoint (``asset_ips``, ``asset_mac_addresses``), non-empty values, ten at most."""
    values = value if isinstance(value, list) else [value] if value else []
    return [text for text in dict.fromkeys(str(v).strip()[:64] for v in values if v is not None) if text][:10]


#: OpenAEV ``asset_status`` values (lower-cased) an asset row keeps (#1134 S4, CTEM-27).
OPENAEV_ASSET_STATUSES: frozenset[str] = frozenset({"active", "inactive", "agentless", "stale"})


def openaev_last_seen(value: Any) -> str:
    """OpenAEV's ``asset_last_seen`` (an ISO instant or epoch seconds) as ISO 8601 UTC, "" when unreadable."""
    if value is None or value == "":
        return ""
    try:
        if isinstance(value, (int, float)):
            seen = datetime.fromtimestamp(float(value), tz=UTC)
        else:
            seen = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
            seen = seen.replace(tzinfo=UTC) if seen.tzinfo is None else seen.astimezone(UTC)
    except (ValueError, OverflowError, OSError):
        return ""
    return seen.isoformat(timespec="seconds").replace("+00:00", "Z")


def openaev_asset_group(group: dict[str, Any]) -> AssetGroupEntry | None:
    """An OpenAEV asset group as an ``asset_groups`` entry."""
    name = str(group.get("asset_group_name") or "").strip()
    group_id = str(group.get("asset_group_id") or "").strip()
    if not name or not group_id:
        return None
    description = str(group.get("asset_group_description") or "").strip()
    return AssetGroupEntry(
        name=name[:200], description=description[:1000], source="openaev", external_id=group_id[:200]
    )


def openaev_ai_system(asset: dict[str, Any]) -> AssetEntry | None:
    """An OpenAEV AI target as an asset of category ``ai_target``, every field kept (#1501; no forced kind)."""
    if not _lower(asset.get("asset_category")):
        asset = {**asset, "asset_category": "ai_target"}
    row = openaev_asset(asset)
    if row is None or row.category != "ai_target":
        return None
    return row


def _lower(value: Any) -> str:
    return str(value or "").strip().lower()


def opencti_system_has_type(node: dict[str, Any]) -> bool:
    """Whether a System names its own category or subcategory (else the batch type applies)."""
    return _lower(node.get("x_opencti_system_category")) in ASSET_TAXONOMY or bool(
        ASSET_SUBCATEGORIES.get(_lower(node.get("x_opencti_system_subcategory")))
    )


def opencti_system(node: dict[str, Any], batch_type: str) -> AssetEntry | None:
    """An OpenCTI System as an asset row.

    Since OpenCTI-Platform/opencti#19943 a System carries the OpenAEV asset
    taxonomy: its own category, subcategory, criticality, exposure,
    environment, owner and platform are mapped. The batch's type is the
    fallback of a System with no category (an older OpenCTI, or a System not
    classified yet); ``createdBy`` is the fallback owner. The platform and the
    markings have no profile field: they are kept as the tags
    ``platform:<value>`` and ``marking:<definition>``.
    """
    name = str(node.get("name") or "").strip()
    system_id = str(node.get("id") or "").strip()
    if not name or not system_id:
        return None
    category = _lower(node.get("x_opencti_system_category"))
    subcategory = _lower(node.get("x_opencti_system_subcategory"))
    if category in ASSET_TAXONOMY:
        subcategory = subcategory if ASSET_SUBCATEGORIES.get(subcategory) == category else ""
    elif ASSET_SUBCATEGORIES.get(subcategory):
        category = ASSET_SUBCATEGORIES[subcategory]
    else:
        category, subcategory = batch_taxonomy(batch_type) or ("generic_asset", "")
    criticality = _lower(node.get("x_opencti_criticality"))
    environment = _lower(node.get("x_opencti_environment"))
    internet_facing = _lower(node.get("x_opencti_internet_facing"))
    owner = (
        str(node.get("x_opencti_owner") or "").strip() or str(((node.get("createdBy") or {}).get("name")) or "").strip()
    )
    tags = [str((label or {}).get("value") or "") for label in (node.get("objectLabel") or [])]
    platform = str(node.get("x_opencti_platform") or "").strip()
    if platform:
        tags.append(f"platform:{platform}")
    tags.extend(
        f"marking:{(m or {}).get('definition')}"
        for m in (node.get("objectMarking") or [])
        if (m or {}).get("definition")
    )
    return AssetEntry(
        name=name[:200],
        category=category,  # type: ignore[arg-type]
        subcategory=subcategory,
        criticality=criticality if criticality in ASSET_CRITICALITIES else "unknown",  # type: ignore[arg-type]
        environment=environment if environment in ASSET_ENVIRONMENTS else ("other" if environment else ""),  # type: ignore[arg-type]
        internet_facing=internet_facing if internet_facing in INTERNET_FACING else "unknown",  # type: ignore[arg-type]
        owner=owner[:200],
        tags=tags[:50],
        source="opencti",
        external_id=system_id[:200],
    )


def opencrq_jewel(asset: dict[str, Any], min_level: str) -> CrownJewel | None:
    """An OpenCRQ supporting asset at or above *min_level* as a crown jewel."""
    from app.services.organization_context_opencrq import _asset_name

    level = str(asset.get("criticality") or "").lower()
    if level not in OPENCRQ_LEVELS or OPENCRQ_LEVELS.index(level) < OPENCRQ_LEVELS.index(min_level):
        return None
    name = _asset_name(asset)
    asset_id = str(asset.get("id") or "")
    if not name or not asset_id:
        return None
    return CrownJewel(name=name[:200], criticality=FROM_OPENCRQ[level], source="opencrq", external_id=asset_id[:200])  # type: ignore[arg-type]


# ── Merge (no I/O) ────────────────────────────────────────────────────


#: section -> (profile field, the row's name attribute, most rows the field holds, the fields a person may
#: set on a synced row that the source does not send: kept across runs as local enrichment, #1134).
SOURCED_FIELDS: dict[str, tuple[str, str, int, tuple[str, ...]]] = {
    "assets": ("assets_exposure", "name", 5000, ("environment", "owner", "tech_stack", "group")),
    "asset_groups": ("asset_groups", "name", 500, ("environment", "owner", "tech_stack", "description")),
    # #1501: the AI target assets, read and written like assets (the alias touches no other asset).
    "ai_systems": ("assets_exposure", "name", 5000, ("environment", "owner", "tech_stack", "group")),
    "crown_jewels": ("crown_jewels", "name", 200, ("owner", "business_service", "asset_names")),
    "tech_stack": ("tech_stack", "product", 500, ("vendor", "cpe", "version")),
}
#: Sections where a row of another source with the same name is a second record of the same thing:
#: added beside it, never skipped; the canonical asset store merges the records (#1178, S3).
MULTI_SOURCE_SECTIONS: frozenset[str] = frozenset({"assets", "ai_systems"})
#: Fields kept from the stored row whatever the source sends: the source never owns them.
_ALWAYS_LOCAL: frozenset[str] = frozenset({"business_service", "asset_names"})


def plan_import(
    profile: OrganizationProfile,
    section: str,
    source: str,
    read: ReadResult,
    *,
    remove_gone: bool = False,
    excluded_ids: list[str] | None = None,
    sync_id: str = "",
) -> ImportPlan:
    """What importing *read* into *section* would do to *profile* (no I/O, *profile* untouched).

    Every list section is keyed on ``source`` + ``external_id`` (#1134). Rows
    this source wrote before that the read no longer returns are listed in
    ``gone``; they are removed when *remove_gone* (a sync's default since
    #1134). A truncated or failed read never removes anything: the rows past
    the cut are not known to be gone. A row whose id is in *excluded_ids* (the
    sync's exclusion list) is neither added nor kept.
    """
    plan = ImportPlan(
        section=section,
        source=source,
        source_total=read.total,
        read=len(read.rows),
        truncated=read.truncated,
        error=read.error,
        remove_gone=remove_gone and not read.truncated and not read.error,
        batch_typed=read.batch_typed,
        sync_id=sync_id[:64],
        software=read.software,
        software_rows=read.software_rows,
        software_unread=read.software_unread,
        software_error=read.software_error,
        findings=read.findings,
        repositories=read.repositories,
    )
    draft = profile.model_copy(deep=True)
    if section in SOURCED_FIELDS:
        _merge_sourced(draft, section, read.rows, source, plan, {str(i) for i in excluded_ids or [] if str(i)})
    elif section == "pirs":
        _merge_pirs(draft, read.rows, plan)
    plan.sample = [
        row.model_dump(mode="json") if hasattr(row, "model_dump") else row for row in read.rows[:SAMPLE_SIZE]
    ]
    plan.profile = draft
    return plan


def _merge_sourced(
    draft: OrganizationProfile, section: str, rows: list[Any], source: str, plan: ImportPlan, excluded: set[str]
) -> None:
    """Merge *rows* of *source* into a list section keyed on ``source`` + ``external_id`` (#1134).

    A row of this source updates its stored row, the local enrichment fields
    (:data:`SOURCED_FIELDS`) kept when the source sends none; a new row named
    like a row a person, an agent or another source wrote is skipped (never
    overwritten), except an asset: a row of another source named alike is a
    second record of the same thing, added beside it for the canonical asset
    store to merge by identity (#1178, S3); a row of the sync's exclusion list
    is left out, and so is its stored copy.
    """
    attr, name_key, cap, local = SOURCED_FIELDS[section]
    current: list[Any] = list(getattr(draft, attr))
    if section == AI_SYSTEMS_ALIAS:
        # The alias merges into the assets and must never treat the other assets of the source as gone.
        others = [r for r in current if r.category != "ai_target"]
        current = [r for r in current if r.category == "ai_target"]
    else:
        others = []
    by_key = {(r.source, r.external_id): i for i, r in enumerate(current) if r.external_id}
    multi = section in MULTI_SOURCE_SECTIONS
    names = {getattr(r, name_key).casefold() for r in current if not multi or r.source == source}
    stamp = {"sync_id": plan.sync_id} if plan.sync_id else {}
    read_ids: set[str] = set()
    for row in rows:
        name = getattr(row, name_key)
        read_ids.add(row.external_id)
        if row.external_id in excluded:
            plan.excluded.append(name)
            continue
        index = by_key.get((source, row.external_id))
        if index is not None:
            before = current[index]
            kept = {
                f: getattr(before, f)
                for f in local
                if f in _ALWAYS_LOCAL or (not getattr(row, f) and getattr(before, f))
            }
            merged = row.model_copy(update={**kept, "sync_id": plan.sync_id or before.sync_id})
            if merged == before:
                plan.unchanged.append(name)
            else:
                current[index] = merged
                plan.updated.append(name)
            continue
        if name.casefold() in names:
            plan.skipped.append(name)
            continue
        current.append(row.model_copy(update=stamp))
        names.add(name.casefold())
        plan.new.append(name)
    dropped: set[int] = set()
    if excluded:
        stored = [r for r in current if r.source == source and r.external_id in excluded]
        plan.excluded_removed = [getattr(r, name_key) for r in stored]
        dropped |= {id(r) for r in stored}
    if not plan.truncated and not plan.error:
        gone = [
            r
            for r in current
            if r.source == source and r.external_id and r.external_id not in read_ids and id(r) not in dropped
        ]
        plan.gone = [getattr(r, name_key) for r in gone]
        if plan.remove_gone:
            # A sync removes only the rows it wrote itself (#1473): a row another
            # writer keyed on this source (a reconcile pull, a one-off import, a
            # row read as a platform row) is reported gone and kept.
            owned = [r for r in gone if not plan.sync_id or r.sync_id == plan.sync_id]
            plan.gone_kept = [getattr(r, name_key) for r in gone if id(r) not in {id(o) for o in owned}]
            dropped |= {id(r) for r in owned}
    if dropped:
        current = [r for r in current if id(r) not in dropped]
    setattr(draft, attr, (others + current)[:cap])


#: Sections whose rows are asset records since #1543 (planned and written by ``(source, external_id)``).
RECORD_SECTIONS: frozenset[str] = frozenset({"assets", "asset_groups"})


@dataclass
class StoredRow:
    """A stored record of the import's source, as :func:`plan_record_import` needs it."""

    external_id: str
    name: str
    sync_id: str = ""


def plan_record_import(
    section: str,
    source: str,
    read: ReadResult,
    *,
    validate: Any,
    stored: dict[tuple[str, str], dict[str, Any]],
    names: dict[str, str],
    source_rows: list[StoredRow],
    remove_gone: bool = False,
    excluded_ids: list[str] | None = None,
    sync_id: str = "",
    organization_id: uuid.UUID,
) -> ImportPlan:
    """What importing *read* into the asset or group records does (no I/O, #1543).

    The merge rules of :func:`_merge_sourced`, against the records instead of
    the profile: *stored* holds the records the read rows address, by
    ``(source, external_id)``, as profile rows; *names* the name keys a new row
    would clash with (for assets, the records of *source* only); *source_rows*
    every record of *source* (read only when gone rows can be known: a
    complete read, or a sync's exclusion list). *validate* is the record
    service's validation, so a row equal to its record is ``unchanged`` and
    never written. No cap: the tables hold what the profile could not.
    """
    from app.services.organization_context_rows import name_key

    plan = ImportPlan(
        section=section,
        source=source,
        source_total=read.total,
        read=len(read.rows),
        truncated=read.truncated,
        error=read.error,
        remove_gone=remove_gone and not read.truncated and not read.error,
        batch_typed=read.batch_typed,
        sync_id=sync_id[:64],
        software=read.software,
        software_rows=read.software_rows,
        software_unread=read.software_unread,
        software_error=read.software_error,
        findings=read.findings,
        repositories=read.repositories,
        organization_id=organization_id,
        members={} if read.error else read.members,
        members_source=read.members_source,
    )
    excluded = {str(i) for i in excluded_ids or [] if str(i)}
    _attr, name_attr, _cap, local = SOURCED_FIELDS[section]
    taken = dict(names)
    read_ids: set[str] = set()
    seen: set[str] = set()
    for row in read.rows:
        value = row.model_dump(mode="json") if hasattr(row, "model_dump") else dict(row)
        name = str(value.get(name_attr) or "")
        external_id = str(value.get("external_id") or "")
        read_ids.add(external_id)
        if external_id in excluded:
            plan.excluded.append(name)
            continue
        if external_id in seen:
            continue  # a source answering one id twice: the first row counts, as in the profile merge
        seen.add(external_id)
        before = stored.get((source, external_id))
        if before is not None:
            kept = {f: before.get(f) for f in local if f in _ALWAYS_LOCAL or (not value.get(f) and before.get(f))}
            merged = validate({**value, **kept, "sync_id": plan.sync_id or before.get("sync_id") or ""})
            if merged == before:
                plan.unchanged.append(name)
            else:
                plan.writes.append(merged)
                plan.updated.append(name)
            continue
        if name_key(name) in taken:
            plan.skipped.append(name)
            continue
        taken[name_key(name)] = source
        plan.writes.append(validate({**value, **({"sync_id": plan.sync_id} if plan.sync_id else {})}))
        plan.new.append(name)
    dropped: set[str] = set()
    if excluded:
        gone_excluded = [r for r in source_rows if r.external_id in excluded]
        plan.excluded_removed = [r.name for r in gone_excluded]
        dropped = {r.external_id for r in gone_excluded}
    if not plan.truncated and not plan.error:
        gone = [r for r in source_rows if r.external_id not in read_ids and r.external_id not in dropped]
        plan.gone = [r.name for r in gone]
        if plan.remove_gone:
            # A sync removes only the rows it wrote itself (#1473), as the profile merge does.
            owned = [r for r in gone if not plan.sync_id or r.sync_id == plan.sync_id]
            owned_ids = {r.external_id for r in owned}
            plan.gone_kept = [r.name for r in gone if r.external_id not in owned_ids]
            dropped |= owned_ids
    plan.remove_refs = [(source, e) for e in sorted(dropped)]
    plan.sample = [
        row.model_dump(mode="json") if hasattr(row, "model_dump") else row for row in read.rows[:SAMPLE_SIZE]
    ]
    return plan


def _merge_pirs(draft: OrganizationProfile, rows: list[dict[str, str]], plan: ImportPlan) -> None:
    ids = list(draft.pir_ids)
    names = dict(draft.pir_names)
    for row in rows:
        if row["id"] in ids:
            if row["name"] != row["id"] and not names.get(row["id"]):
                names[row["id"]] = row["name"][:300]
                plan.updated.append(row["name"])
            else:
                plan.unchanged.append(row["name"])
            continue
        ids.append(row["id"])
        if row["name"] != row["id"]:
            names[row["id"]] = row["name"][:300]
        plan.new.append(row["name"])
    draft.pir_ids = ids[:100]
    draft.pir_names = {k: v for k, v in names.items() if k in draft.pir_ids}


# ── Filters (no I/O) ──────────────────────────────────────────────────


def _values(filters: dict[str, Any], key: str) -> list[str]:
    raw = filters.get(key)
    if raw is None or raw == "":
        return []
    items = raw if isinstance(raw, list) else [raw]
    return [str(v).strip() for v in items if str(v).strip()]


def openaev_filter_group(filters: dict[str, Any], *, asset_ids: list[str] | None = None) -> dict[str, Any] | None:
    """The OpenAEV ``filterGroup`` of an asset import: every filter given, ANDed."""
    parts: list[dict[str, Any]] = []
    if asset_ids is not None:
        parts.append({"key": "asset_id", "mode": "or", "operator": "eq", "values": asset_ids})
    for key, field_name, operator in (
        ("tags", "asset_tags", "contains"),
        ("category", "asset_category", "eq"),
        ("subcategory", "asset_subcategory", "eq"),
        ("criticality", "asset_criticality", "eq"),
        ("platform", "endpoint_platform", "eq"),
        ("type", "security_platform_type", "eq"),
    ):
        values = _values(filters, key)
        if values:
            parts.append({"key": field_name, "mode": "or", "operator": operator, "values": values})
    return {"mode": "and", "filters": parts} if parts else None


def opencti_filter_group(filters: dict[str, Any]) -> dict[str, Any] | None:
    """OpenCTI's ``FilterGroup`` for a Systems import: labels, markings, author, creation date.

    A ``filter_group`` given as is (built with OpenCTI's own filter editor) is
    nested under the generated one, so both apply.
    """
    parts: list[dict[str, Any]] = []
    keyed = [
        ("labels", "objectLabel"),
        ("markings", "objectMarking"),
        ("created_by", "createdBy"),
        ("creator", "creator_id"),
        *OPENCTI_SYSTEM_ATTRIBUTES.items(),
    ]
    for key, field_name in keyed:
        values = _values(filters, key)
        if values:
            parts.append({"key": [field_name], "values": values, "operator": "eq", "mode": "or"})
    created_after = str(filters.get("created_after") or "").strip()
    if created_after:
        parts.append({"key": ["created"], "values": [created_after], "operator": "gte", "mode": "or"})
    nested = filters.get("filter_group")
    groups = [nested] if isinstance(nested, dict) and (nested.get("filters") or nested.get("filterGroups")) else []
    if not parts and not groups:
        return None
    return {"mode": "and", "filters": parts, "filterGroups": groups}


# ── Sources (I/O) ─────────────────────────────────────────────────────


async def source_statuses(db: AsyncSession, *, user_id: uuid.UUID) -> dict[str, SourceStatus]:
    """Whether each platform is connected for this administrator (no platform is assumed installed)."""
    from app.services.ctem_metrics_collector import _user_creds
    from app.services.organization_context_opencrq import _opencrq_connection

    statuses: dict[str, SourceStatus] = {}
    for source in ("openaev", "opencti"):
        try:
            creds = await _user_creds(db, str(user_id), source)
        except Exception as exc:  # one platform never hides the others
            logger.warning("Organization import: %s status unreadable: %s", source, format_exception_message(exc))
            creds = None
        statuses[source] = SourceStatus(
            source=source,
            available=creds is not None,
            reason="" if creds is not None else "not connected, or no token for this administrator",
        )
    try:
        connection = await _opencrq_connection(db, str(user_id))
    except Exception as exc:
        logger.warning("Organization import: OpenCRQ status unreadable: %s", format_exception_message(exc))
        connection = "unreadable"
    statuses["opencrq"] = SourceStatus(
        source="opencrq",
        available=not isinstance(connection, str),
        reason=connection if isinstance(connection, str) else "",
    )
    from app.services import organization_context_microsoft as microsoft

    for source in microsoft.SOURCES:
        try:
            available, reason, code = await microsoft.connection_status(db, str(user_id), source)
        except Exception as exc:
            logger.warning("Organization import: %s status unreadable: %s", source, format_exception_message(exc))
            available, reason, code = False, "unreadable", ""
        statuses[source] = SourceStatus(source=source, available=available, reason=reason, reason_code=code)
    from app.services import organization_context_endpoints as endpoints

    for source in endpoints.SOURCES:
        try:
            available, reason, code = await endpoints.connection_status(db, str(user_id), source)
        except Exception as exc:
            logger.warning("Organization import: %s status unreadable: %s", source, format_exception_message(exc))
            available, reason, code = False, "unreadable", ""
        statuses[source] = SourceStatus(source=source, available=available, reason=reason, reason_code=code)
    from app.services import organization_context_code as code_sources

    for source in code_sources.SOURCES:
        try:
            available, reason, code = await code_sources.connection_status(db, str(user_id), source)
        except Exception as exc:
            logger.warning("Organization import: %s status unreadable: %s", source, format_exception_message(exc))
            available, reason, code = False, "unreadable", ""
        statuses[source] = SourceStatus(source=source, available=available, reason=reason, reason_code=code)
    return statuses


async def _openaev_search(
    base_url: str, headers: dict[str, Any], path: str, filter_group: dict[str, Any] | None, text: str = ""
) -> ReadResult:
    from app.integrations.providers.openaev._common import _api, _search_body

    result = ReadResult()
    page = 0
    items: list[dict[str, Any]] = []
    while len(items) < MAX_ROWS:
        resp, err = await _api(
            base_url,
            headers,
            "post",
            path,
            "organization import",
            json=_search_body(page=page, size=200, filter_group=filter_group, text_search=text),
            retry_transient=True,
        )
        if err or resp is None:
            result.error = f"OpenAEV: {err or 'no answer'}"
            return result
        data = resp.json()
        content = (data.get("content") if isinstance(data, dict) else data) or []
        if result.total is None and isinstance(data, dict) and data.get("totalElements") is not None:
            result.total = int(data["totalElements"])
        items.extend(i for i in content if isinstance(i, dict))
        if len(content) < 200 or (result.total is not None and len(items) >= result.total):
            break
        page += 1
    result.truncated = result.total is not None and result.total > MAX_ROWS
    result.rows = items[:MAX_ROWS]
    return result


def _reads_ai_targets(filters: dict[str, Any], batch_type: str) -> bool:
    """Whether an OpenAEV assets read includes the AI targets: no endpoint-only filter, no other batch type."""
    picked = batch_taxonomy(batch_type) if batch_type else None
    if picked and picked[0] != "ai_target":
        return False
    if _values(filters, "platform"):
        return False
    categories = [c.lower() for c in _values(filters, "category")]
    return not categories or "ai_target" in categories


#: OpenAEV ``asset_type`` values another read already serves whole: the endpoints (``/api/endpoints/search``,
#: hostname, IPs and MACs included) and the AI targets (their own section).
_OPENAEV_TYPES_READ_ELSEWHERE: frozenset[str] = frozenset({"endpoint", "ai_target"})


async def _openaev_inventory(
    base_url: str, headers: dict[str, Any], filter_group: dict[str, Any] | None, *, endpoints_only: bool = False
) -> ReadResult:
    """Every OpenAEV asset of an assets import: the endpoints whole, then the rest of the inventory.

    ``/api/endpoints/search`` answers the endpoints only; a web application, a cloud resource, a SaaS
    application or a network device created as a generic asset lives in the unified inventory
    (``/api/assets/search``), thin (no hostname) but named and categorised. Without this second read
    those assets never reached the XTM One inventory, so no other product could find them there.
    A platform filter names endpoints alone: the second read is skipped.
    """
    read = await _openaev_search(base_url, headers, "/api/endpoints/search", filter_group)
    if read.error or endpoints_only:
        return read
    rest = await _openaev_search(base_url, headers, "/api/assets/search", filter_group)
    if rest.error:
        return ReadResult(error=rest.error)
    seen = {str(r.get("asset_id") or "") for r in read.rows}
    others = [
        r
        for r in rest.rows
        if str(r.get("asset_type") or "").strip().lower() not in _OPENAEV_TYPES_READ_ELSEWHERE
        and str(r.get("asset_id") or "") not in seen
    ]
    read.rows = (read.rows + others)[:MAX_ROWS]
    read.truncated = read.truncated or rest.truncated or (bool(others) and len(read.rows) >= MAX_ROWS)
    if read.total is not None:
        read.total += len(others)
    return read


async def _openaev_members_by_group(
    base_url: str, headers: dict[str, Any], group_ids: list[str]
) -> tuple[dict[str, list[str]], str | None]:
    """The asset ids of each group, static members and dynamic matches alike (a dynamic group resolved)."""
    from app.integrations.providers.openaev._common import _api

    out: dict[str, list[str]] = {}
    for group_id in group_ids:
        resp, err = await _api(
            base_url, headers, "get", f"/api/asset_groups/{group_id}", "read asset group", retry_transient=True
        )
        if err or resp is None:
            return {}, f"OpenAEV asset group {group_id}: {err or 'no answer'}"
        data = resp.json() if resp is not None else {}
        ids = [
            str(a) for key in ("asset_group_assets", "asset_group_dynamic_assets") for a in (data.get(key) or []) if a
        ]
        out[group_id] = list(dict.fromkeys(ids))
    return out, None


async def _openaev_group_members(
    base_url: str, headers: dict[str, Any], group_ids: list[str]
) -> tuple[list[str], str | None]:
    """Every asset id of the given groups, static members and dynamic matches alike."""
    by_group, err = await _openaev_members_by_group(base_url, headers, group_ids)
    if err:
        return [], err
    return list(dict.fromkeys(m for ids in by_group.values() for m in ids)), None


async def _read_openaev(
    db: AsyncSession, user_id: str, section: str, filters: dict[str, Any], batch_type: str
) -> ReadResult:
    from app.services.ctem_metrics_collector import _user_creds

    creds = await _user_creds(db, user_id, "openaev")
    if creds is None:
        return ReadResult(error="OpenAEV: not connected, or no token for this administrator")
    base_url, headers = creds
    text = str(filters.get("search") or "").strip()
    if section == "assets":
        asset_ids = None
        groups = _values(filters, "asset_groups")
        if groups:
            asset_ids, err = await _openaev_group_members(base_url, headers, groups)
            if err:
                return ReadResult(error=err)
            if not asset_ids:
                return ReadResult(total=0)
        read = await _openaev_inventory(
            base_url,
            headers,
            openaev_filter_group(filters, asset_ids=asset_ids),
            endpoints_only=bool(_values(filters, "platform")),
        )
        read.rows = [r for r in (openaev_asset(a, batch_type) for a in read.rows) if r]
        if read.error or not _reads_ai_targets(filters, batch_type):
            return read
        # #1501: OpenAEV's AI targets are assets too; an assets sync keeps them (and never drops them as gone).
        targets = await _openaev_search(
            base_url, headers, "/api/ai_targets/search", openaev_filter_group(filters, asset_ids=asset_ids)
        )
        if targets.error:
            return ReadResult(error=targets.error)
        read.rows += [r for r in (openaev_ai_system(a) for a in targets.rows) if r]
        read.truncated = read.truncated or targets.truncated
        if read.total is not None and targets.total is not None:
            read.total += targets.total
        return read
    if section == "asset_groups":
        tags = _values(filters, "tags")
        group = (
            {
                "mode": "and",
                "filters": [{"key": "asset_group_tags", "mode": "or", "operator": "contains", "values": tags}],
            }
            if tags
            else None
        )
        read = await _openaev_search(base_url, headers, "/api/asset_groups/search", group, text)
        read.rows = [r for r in (openaev_asset_group(g) for g in read.rows) if r]
        if read.error or not read.rows:
            return read
        # #1556: each group's members, kept in sync as memberships of the OpenAEV asset records.
        members, err = await _openaev_members_by_group(base_url, headers, [r.external_id for r in read.rows])
        if err:
            read.error = err
            return read
        read.members, read.members_source = members, "openaev"
        return read
    if section == AI_SYSTEMS_ALIAS:
        # #1501: OpenAEV's AI targets (``/api/ai_targets``), as assets of category ai_target.
        read = await _openaev_search(base_url, headers, "/api/ai_targets/search", openaev_filter_group(filters), text)
        read.rows = [r for r in (openaev_ai_system(a) for a in read.rows) if r]
        return read
    if section == "tech_stack":
        read = await _openaev_search(base_url, headers, "/api/security_platforms/search", openaev_filter_group(filters))
        seen: set[str] = set()
        rows: list[TechStackEntry] = []
        for item in read.rows:
            product = str(item.get("asset_name") or item.get("security_platform_name") or "").strip()
            if product and product.casefold() not in seen:
                seen.add(product.casefold())
                # A product is keyed on its name: several security platforms may run one product.
                rows.append(
                    TechStackEntry(product=product[:200], source="openaev", external_id=product.casefold()[:200])
                )
        read.rows = rows
        return read
    return ReadResult(error=f"OpenAEV cannot feed {section}")


_OCTI_SYSTEM_BASE = "id name createdBy { name } objectLabel { value } objectMarking { definition }"
_OCTI_SYSTEM_TAXONOMY = " ".join(OPENCTI_SYSTEM_ATTRIBUTES.values()) + " x_opencti_owner"


def opencti_systems_query(taxonomy: bool) -> str:
    """The Systems page query; with *taxonomy*, the #19943 attributes too."""
    fields = f"{_OCTI_SYSTEM_BASE} {_OCTI_SYSTEM_TAXONOMY}" if taxonomy else _OCTI_SYSTEM_BASE
    return (
        "query XtmOrganizationImportSystems($first: Int, $after: ID, $filters: FilterGroup) {\n"
        "  systems(first: $first, after: $after, filters: $filters) {\n"
        f"    edges {{ node {{ {fields} }} }}\n"
        "    pageInfo { globalCount hasNextPage endCursor }\n"
        "  }\n}\n"
    )


#: One System with the attributes the import reads: no introspection, which OpenCTI disables by default
#: since OpenCTI-Platform/opencti#19370.
_OCTI_SYSTEM_PROBE = (
    "query XtmOrganizationImportSystemTaxonomy {\n"
    f"  systems(first: 1) {{ edges {{ node {{ id {_OCTI_SYSTEM_TAXONOMY} }} }} }}\n"
    "}\n"
)
#: The names an OpenCTI before #19943 refuses in the probe: an attribute, or the Systems list itself.
_OCTI_SYSTEM_PROBE_NAMES = (*OPENCTI_SYSTEM_ATTRIBUTES.values(), "x_opencti_owner", "systems")


def opencti_lacks_system_taxonomy(error: str) -> bool:
    """True when OpenCTI refused the probe because its schema predates #19943 (a validation error)."""
    lowered = (error or "").lower()
    if 'unknown type "system"' in lowered:
        return True
    return "cannot query field" in lowered and any(f'"{name}"' in lowered for name in _OCTI_SYSTEM_PROBE_NAMES)


async def opencti_has_system_taxonomy(client: Any, base_url: str, headers: dict[str, Any]) -> tuple[bool, str | None]:
    """Whether this OpenCTI carries the System taxonomy of OpenCTI-Platform/opencti#19943.

    Answers ``(True, None)`` when the probe runs (an empty list included), ``(False, None)`` when OpenCTI
    refuses the taxonomy fields as unknown (an older OpenCTI), and ``(False, error)`` on any other failure
    (authentication, network, server error): an unreadable OpenCTI is never read as an older one.
    """
    from app.integrations.providers.opencti._common import _graphql

    _data, err = await _graphql(client, base_url, headers, _OCTI_SYSTEM_PROBE, {})
    if not err:
        return True, None
    if opencti_lacks_system_taxonomy(err):
        return False, None
    logger.warning("Organization import: OpenCTI System taxonomy probe failed: %s", err)
    return False, err


async def _read_opencti(
    db: AsyncSession, user_id: str, section: str, filters: dict[str, Any], batch_type: str
) -> ReadResult:
    from app.integrations.providers.opencti._common import _client, _graphql
    from app.services.ctem_metrics_collector import _user_creds
    from app.services.organization_context_draft import opencti_pirs, read_all_pirs

    creds = await _user_creds(db, user_id, "opencti")
    if creds is None:
        return ReadResult(error="OpenCTI: not connected, or no token for this administrator")
    base_url, headers = creds
    result = ReadResult()
    async with _client(headers, timeout=30) as client:
        if section == "pirs":
            data, err = await read_all_pirs(client, base_url, headers)
            if err:
                return ReadResult(error=f"OpenCTI PIRs: {err}")
            picked = set(_values(filters, "pir_ids"))
            rows = [row for row in opencti_pirs(data) if not picked or row["id"] in picked]
            return ReadResult(rows=rows, total=len(rows))
        if section != "assets":
            return ReadResult(error=f"OpenCTI cannot feed {section}")
        taxonomy, err = await opencti_has_system_taxonomy(client, base_url, headers)
        if err:
            return ReadResult(error=f"OpenCTI: {err}")
        if not taxonomy:
            # An OpenCTI before #19943 rejects a filter on attributes it does not have.
            filters = {k: v for k, v in filters.items() if k not in OPENCTI_SYSTEM_ATTRIBUTES}
        query = opencti_systems_query(taxonomy)
        variables: dict[str, Any] = {"first": 200, "after": None, "filters": opencti_filter_group(filters)}
        nodes: list[dict[str, Any]] = []
        while len(nodes) < MAX_ROWS:
            data, err = await _graphql(client, base_url, headers, query, variables)
            if err:
                return ReadResult(error=f"OpenCTI: {err}")
            page = (data or {}).get("systems") or {}
            info = page.get("pageInfo") or {}
            if result.total is None and info.get("globalCount") is not None:
                result.total = int(info["globalCount"])
            nodes.extend((edge or {}).get("node") or {} for edge in page.get("edges") or [])
            if not info.get("hasNextPage") or not info.get("endCursor"):
                break
            variables["after"] = info["endCursor"]
    result.truncated = result.total is not None and result.total > MAX_ROWS
    result.rows = [r for r in (opencti_system(n, batch_type) for n in nodes[:MAX_ROWS]) if r]
    result.batch_typed = sum(1 for n in nodes[:MAX_ROWS] if not opencti_system_has_type(n))
    return result


async def _read_opencrq(db: AsyncSession, user_id: str, filters: dict[str, Any]) -> ReadResult:
    from app.integrations.providers.opencrq._common import _resolve_tenant
    from app.services.organization_context_opencrq import _list_assets, _opencrq_connection

    connection = await _opencrq_connection(db, user_id)
    if isinstance(connection, str):
        return ReadResult(error=f"OpenCRQ: {connection}")
    base_url, headers = connection
    tenant, err = await _resolve_tenant(base_url, headers, str(filters.get("tenant_id") or ""))
    if err:
        return ReadResult(error=f"OpenCRQ: {err}")
    assets, err = await _list_assets(base_url, headers, tenant or "")
    if err:
        return ReadResult(error=f"OpenCRQ: {err}")
    level = str(filters.get("min_criticality") or "high")
    if level not in OPENCRQ_LEVELS:
        level = "high"
    rows = [j for j in (opencrq_jewel(a, level) for a in assets) if j]
    rows.sort(key=lambda j: ("critical", "high", "medium", "low").index(j.criticality))
    return ReadResult(rows=rows, total=len(rows))


async def _read_microsoft(db: AsyncSession, user_id: str, source: str, filters: dict[str, Any]) -> ReadResult:
    """Devices (and their software) from Microsoft Intune or Defender for Endpoint (#1251)."""
    from app.services import organization_context_microsoft as microsoft

    token, reason = await microsoft.access_token(db, user_id, source)
    if not token:
        return ReadResult(error=f"{source}: {reason}")
    reader = microsoft.read_intune if source == microsoft.INTUNE else microsoft.read_defender
    read = await reader(token, filters, limit=MAX_ROWS)
    result = ReadResult(total=read.total, truncated=read.truncated, error=read.error)
    if read.error:
        return result
    for row in read.rows:
        try:
            result.rows.append(AssetEntry.model_validate(row))
        except ValueError as exc:  # one bad device never stops the others
            logger.info("Organization import: %s device %s refused: %s", source, row.get("external_id"), exc)
    kept = {r.external_id for r in result.rows}
    result.software = {k: v for k, v in read.software.items() if k in kept}
    result.software_rows = sum(len(v) for v in result.software.values())
    result.software_unread = {k for k in read.software_unread if k in kept}
    result.software_error = read.software_error if result.software_unread else ""
    return result


async def _read_endpoint(db: AsyncSession, user_id: str, source: str, filters: dict[str, Any]) -> ReadResult:
    """Devices from Tanium, CrowdStrike Falcon or ServiceNow CMDB (#1556), read with the source's integration."""
    from app.services import organization_context_endpoints as endpoints

    token, reason = await endpoints.credential(db, user_id, source)
    if not token:
        return ReadResult(error=f"{source}: {reason}")
    read = await endpoints.READERS[source](token, filters, limit=MAX_ROWS)
    result = ReadResult(total=read.total, truncated=read.truncated, error=read.error)
    if read.error:
        return result
    for row in read.rows:
        try:
            result.rows.append(AssetEntry.model_validate(row))
        except ValueError as exc:  # one bad device never stops the others
            logger.info("Organization import: %s device %s refused: %s", source, row.get("external_id"), exc)
    kept = {r.external_id for r in result.rows}
    result.software = {k: v for k, v in read.software.items() if k in kept}
    result.software_rows = sum(len(v) for v in result.software.values())
    return result


async def _read_groups(db: AsyncSession, user_id: str, source: str, filters: dict[str, Any]) -> ReadResult:
    """Asset groups and their members from Intune, Tanium, CrowdStrike Falcon or ServiceNow (#1556)."""
    from app.services import organization_context_groups as groups

    read = await groups.read_groups(db, user_id, source, filters, limit=MAX_ROWS)
    result = ReadResult(total=read.total, truncated=read.truncated, error=read.error)
    if read.error:
        return result
    for row in read.rows:
        try:
            result.rows.append(AssetGroupEntry.model_validate(row))
        except ValueError as exc:  # one bad group never stops the others
            logger.info("Organization import: %s group %s refused: %s", source, row.get("external_id"), exc)
    kept = {r.external_id for r in result.rows}
    # A group whose members were not read whole is left out: its stored memberships stay.
    result.members = {k: v for k, v in read.members.items() if k in kept}
    result.members_source = source
    return result


async def _read_code(db: AsyncSession, user_id: str, source: str, filters: dict[str, Any]) -> ReadResult:
    """Repositories (and their dependencies) from GitHub or GitLab, read-only."""
    from app.services import organization_context_code as code_sources

    token, api, reason = await code_sources.access_token(db, user_id, source)
    if not token:
        return ReadResult(error=f"{source}: {reason}")
    read = await code_sources.read_repositories(source, token, api, filters, limit=MAX_ROWS)
    result = ReadResult(total=read.total, truncated=read.truncated, error=read.error)
    if read.error:
        return result
    for row in read.rows:
        try:
            result.rows.append(AssetEntry.model_validate(row))
        except ValueError as exc:  # one bad repository never stops the others
            logger.info("Organization import: %s repository %s refused: %s", source, row.get("external_id"), exc)
    kept = {r.external_id for r in result.rows}
    result.software = {k: v for k, v in read.software.items() if k in kept}
    result.software_rows = sum(len(v) for v in result.software.values())
    result.software_unread = {k for k in read.software_unread if k in kept}
    result.findings = {k: v for k, v in read.findings.items() if k in kept}
    result.repositories = {
        r.external_id: {"name": r.name, "identity_key": r.cloud_resource_id} for r in result.rows if r.external_id
    }
    return result


async def store_endpoint_software(db: AsyncSession, plan: ImportPlan, *, user_id: Any = None) -> int:
    """After a saved plan of an endpoint source, store each device's software and rebuild the store.

    The software of a device the source no longer returns goes with it when the
    read was complete; a device whose software read failed keeps what was stored.
    A code source's dependencies are then matched to OpenCTI vulnerabilities with
    *user_id*'s own credential, after the caller's commit (#1331, slice 4).
    Answers the documents written or removed (0: nothing to do).
    """
    from app.services import organization_asset_store as store
    from app.services.organizations import get_primary

    if plan.source not in store.ENDPOINT_SOFTWARE_FORMATS or plan.error:
        return 0
    primary = await get_primary(db)
    if primary is None:
        return 0
    changed = await store.replace_endpoint_software(
        db, primary.id, plan.source, plan.software, complete=not plan.truncated, unread=plan.software_unread
    )
    if changed:
        await store.rebuild_from_profile(db, primary.id)
    if plan.findings:
        # GitLab dependency vulnerabilities (#1331, slice 3): path 3 signals on the repository.
        from app.services import ctem_code_findings

        await ctem_code_findings.record_findings(db, plan.findings, organization_id=primary.id)
    if plan.repositories and user_id:
        # Dependencies matched to OpenCTI vulnerabilities, PURL first (#1331, slice 4).
        from app.services import ctem_dependency_matches

        ctem_dependency_matches.schedule_after_commit(
            db,
            organization_id=primary.id,
            user_id=str(user_id),
            software=plan.software,
            repositories=plan.repositories,
        )
    return changed


async def read_source(
    db: AsyncSession, *, user_id: uuid.UUID, section: str, source: str, filters: dict[str, Any], batch_type: str = ""
) -> ReadResult:
    """Read *section* from *source*; a failure is answered in ``error``, never raised."""
    if source not in SECTION_SOURCES.get(section, ()):
        return ReadResult(error=f"{source} cannot feed {section}")
    try:
        if source == "openaev":
            return await _read_openaev(db, str(user_id), section, filters, batch_type)
        if source == "opencti":
            return await _read_opencti(db, str(user_id), section, filters, batch_type)
        if section == "asset_groups":
            return await _read_groups(db, str(user_id), source, filters)
        if source in ("intune", "defender"):
            return await _read_microsoft(db, str(user_id), source, filters)
        if source in ("tanium", "crowdstrike", "servicenow"):
            return await _read_endpoint(db, str(user_id), source, filters)
        if source in ("github", "gitlab"):
            return await _read_code(db, str(user_id), source, filters)
        return await _read_opencrq(db, str(user_id), filters)
    except Exception as exc:  # a platform that fails is reported, never guessed
        logger.warning("Organization import: %s unreadable: %s", source, format_exception_message(exc))
        return ReadResult(error=f"{source}: {type(exc).__name__}")


# ── Filter options (I/O) ──────────────────────────────────────────────


@dataclass
class FilterOptions:
    """The values a source offers for each filter of a section, and the filters it cannot offer."""

    #: filter key -> [{value, label, description, parent}] (``parent``: the category of a subcategory).
    options: dict[str, list[dict[str, str]]] = field(default_factory=dict)
    #: filter key -> why it is not offered, a code the dialog words: ``not_connected``, ``unreadable`` (the
    #: platform's text stays in the log), ``no_system_taxonomy`` (an OpenCTI before #19943).
    unavailable: dict[str, str] = field(default_factory=dict)


def _option(value: Any, label: Any = None, description: Any = "", parent: Any = "") -> dict[str, str]:
    return {
        "value": str(value),
        "label": str(label or value),
        "description": str(description or ""),
        "parent": str(parent or ""),
    }


def taxonomy_options(upper: bool) -> dict[str, list[dict[str, str]]]:
    """Category, subcategory and criticality options from the profile's own taxonomy (OpenAEV's enums)."""
    case = str.upper if upper else str
    return {
        "category": [_option(case(c)) for c in ASSET_TAXONOMY],
        "subcategory": [_option(case(s), parent=case(c)) for c, subs in ASSET_TAXONOMY.items() for s in subs],
        "criticality": [_option(case(c)) for c in ASSET_CRITICALITIES],
    }


_SUBCATEGORY_PARENT = re.compile(r"\(category:\s*([A-Z_]+)\)\s*$")


def opencti_vocabulary_options(key: str, nodes: list[dict[str, Any]]) -> list[dict[str, str]]:
    """OpenCTI vocabulary entries as options; a subcategory's parent is read from its description (#19943)."""
    rows = []
    for node in nodes:
        name = str(node.get("name") or "").strip()
        if not name:
            continue
        description = str(node.get("description") or "")
        match = _SUBCATEGORY_PARENT.search(description) if key == "subcategory" else None
        rows.append(
            _option(
                name,
                description=_SUBCATEGORY_PARENT.sub("", description).strip(),
                parent=match.group(1) if match else "",
            )
        )
    return rows


_OCTI_OPTIONS = """
query XtmOrganizationImportOptions {
  labels(first: 500, orderBy: value, orderMode: asc) { edges { node { id value } } }
  markingDefinitions(first: 200) { edges { node { id definition } } }
  identities(first: 500, types: ["Organization", "Individual", "System"], orderBy: name, orderMode: asc) {
    edges { node { id name entity_type } }
  }
  creators(entityTypes: ["System"]) { edges { node { id name } } }
}
"""

_OCTI_VOCABULARY = """
query XtmOrganizationImportVocabulary($category: VocabularyCategory!) {
  vocabularies(category: $category, first: 500) { edges { node { name description } } }
}
"""


def _edges(data: dict[str, Any], key: str) -> list[dict[str, Any]]:
    return [(e or {}).get("node") or {} for e in (((data or {}).get(key) or {}).get("edges") or [])]


async def _opencti_options(db: AsyncSession, user_id: str) -> FilterOptions:
    from app.integrations.providers.opencti._common import _client, _graphql
    from app.services.ctem_metrics_collector import _user_creds

    result = FilterOptions()
    creds = await _user_creds(db, user_id, "opencti")
    if creds is None:
        result.unavailable = {k: "not_connected" for k in SOURCE_FILTERS[("assets", "opencti")]}
        return result
    base_url, headers = creds
    async with _client(headers, timeout=30) as client:
        data, err = await _graphql(client, base_url, headers, _OCTI_OPTIONS, {})
        if err:
            logger.warning("Organization import: OpenCTI filter values unreadable: %s", err)
            for key in ("labels", "markings", "created_by", "creator"):
                result.unavailable[key] = "unreadable"
        else:
            result.options["labels"] = [_option(n["id"], n.get("value")) for n in _edges(data, "labels") if n.get("id")]
            result.options["markings"] = [
                _option(n["id"], n.get("definition")) for n in _edges(data, "markingDefinitions") if n.get("id")
            ]
            result.options["created_by"] = [
                _option(n["id"], n.get("name"), n.get("entity_type")) for n in _edges(data, "identities") if n.get("id")
            ]
            result.options["creator"] = [
                _option(n["id"], n.get("name")) for n in _edges(data, "creators") if n.get("id")
            ]
        taxonomy, err = await opencti_has_system_taxonomy(client, base_url, headers)
        if not taxonomy:
            for key in OPENCTI_SYSTEM_ATTRIBUTES:
                result.unavailable[key] = "unreadable" if err else "no_system_taxonomy"
            return result
        for key, category in OPENCTI_SYSTEM_VOCABULARIES.items():
            vocab, err = await _graphql(client, base_url, headers, _OCTI_VOCABULARY, {"category": category})
            if err:
                logger.warning("Organization import: OpenCTI vocabulary %s unreadable: %s", category, err)
                result.unavailable[key] = "unreadable"
                continue
            result.options[key] = opencti_vocabulary_options(key, _edges(vocab, "vocabularies"))
    return result


async def _openaev_options(db: AsyncSession, user_id: str, section: str) -> FilterOptions:
    from app.integrations.providers.openaev._common import _api
    from app.services.ctem_metrics_collector import _user_creds

    result = FilterOptions()
    if section == "assets":
        result.options.update(taxonomy_options(upper=True))
        result.options["platform"] = [_option(p) for p in OPENAEV_PLATFORMS]
    if section == "tech_stack":
        # OpenAEV's security platform types are the subcategories of its security_platform category.
        result.options["type"] = [_option(s.upper()) for s in ASSET_TAXONOMY["security_platform"]]
    wanted = set(SOURCE_FILTERS.get((section, "openaev"), ())) & {"tags", "asset_groups"}
    if not wanted:
        return result
    creds = await _user_creds(db, user_id, "openaev")
    if creds is None:
        result.unavailable = {k: "not_connected" for k in wanted}
        return result
    base_url, headers = creds
    for key, path, id_key, name_key in (
        ("tags", "/api/tags", "tag_id", "tag_name"),
        ("asset_groups", "/api/asset_groups", "asset_group_id", "asset_group_name"),
    ):
        if key not in wanted:
            continue
        resp, err = await _api(base_url, headers, "get", path, f"list {key}", retry_transient=True)
        if err or resp is None:
            logger.warning("Organization import: OpenAEV %s unreadable: %s", key, err or "no answer")
            result.unavailable[key] = "unreadable"
            continue
        items = resp.json()
        items = items.get("content") if isinstance(items, dict) else items
        result.options[key] = [
            _option(i[id_key], i.get(name_key)) for i in (items or []) if isinstance(i, dict) and i.get(id_key)
        ][:1000]
    return result


async def _opencrq_options(db: AsyncSession, user_id: str) -> FilterOptions:
    from app.integrations.providers.opencrq._common import _trpc
    from app.services.organization_context_opencrq import _opencrq_connection

    result = FilterOptions(options={"min_criticality": [_option(level) for level in OPENCRQ_LEVELS]})
    connection = await _opencrq_connection(db, user_id)
    if isinstance(connection, str):
        result.unavailable["tenant_id"] = "not_connected"
        return result
    base_url, headers = connection
    tenants, err = await _trpc(base_url, headers, "tenants.getTenants", "list tenants")
    if err:
        logger.warning("Organization import: OpenCRQ tenants unreadable: %s", err)
        result.unavailable["tenant_id"] = "unreadable"
        return result
    result.options["tenant_id"] = [_option(t["id"], t.get("name")) for t in (tenants or []) if t.get("id")]
    return result


#: The fixed values of the Microsoft endpoint filters (the APIs' own enumerations, #1251).
MICROSOFT_FILTER_VALUES: dict[str, dict[str, tuple[str, ...]]] = {
    "intune": {
        "operating_system": ("Windows", "macOS", "iOS", "iPadOS", "Android", "Linux"),
        "compliance": ("compliant", "noncompliant", "inGracePeriod", "conflict", "error", "unknown"),
        "ownership": ("company", "personal"),
        "last_seen_days": ("7", "30", "90"),
        "include_owner": ("true",),
    },
    "defender": {
        "os_platform": (
            "Windows11",
            "Windows10",
            "WindowsServer2022",
            "WindowsServer2019",
            "macOS",
            "Linux",
            "iOS",
            "Android",
        ),
        "health_status": ("Active", "Inactive", "ImpairedCommunication", "NoSensorData"),
        "exposure_level": ("High", "Medium", "Low", "None"),
        "last_seen_days": ("7", "30", "90"),
    },
}


async def _code_options(db: AsyncSession, user_id: str, source: str) -> FilterOptions:
    """The organizations (GitHub) or groups (GitLab) the account reaches, and the switches."""
    from app.services import organization_context_code as code_sources

    owners = "organizations" if source == "github" else "groups"
    switches = {key: [_option("true")] for key in ("include_archived", "include_forks", "skip_dependencies")}
    try:
        return FilterOptions(options={owners: await code_sources.owner_options(db, user_id, source), **switches})
    except Exception as exc:  # the owners filter says why it is empty, the switches stay
        logger.info("Organization import: %s owners unreadable: %s", source, format_exception_message(exc))
        return FilterOptions(options=switches, unavailable={owners: "unreadable"})


def _endpoint_options(source: str) -> FilterOptions:
    from app.services.organization_context_endpoints import FILTER_VALUES

    return FilterOptions(options={key: [_option(v) for v in values] for key, values in FILTER_VALUES[source].items()})


def _microsoft_options(source: str) -> FilterOptions:
    return FilterOptions(
        options={key: [_option(v) for v in values] for key, values in MICROSOFT_FILTER_VALUES[source].items()}
    )


async def filter_options(db: AsyncSession, *, user_id: uuid.UUID, section: str, source: str) -> FilterOptions:
    """The values each filter of *section* offers in *source*, read live (a failure is a reason, never raised)."""
    if source not in SECTION_SOURCES.get(section, ()):
        return FilterOptions()
    try:
        if source == "opencti":
            return await _opencti_options(db, str(user_id)) if section == "assets" else FilterOptions()
        if source == "openaev":
            return await _openaev_options(db, str(user_id), section)
        if section == "asset_groups":
            return FilterOptions()  # a free text filter (search) only
        if source in ("intune", "defender"):
            return _microsoft_options(source)
        if source in ("tanium", "crowdstrike", "servicenow"):
            return _endpoint_options(source)
        if source in ("github", "gitlab"):
            return await _code_options(db, str(user_id), source)
        return await _opencrq_options(db, str(user_id))
    except Exception as exc:  # the dialog still opens: the filters say why they are empty
        logger.warning("Organization import: %s options unreadable: %s", source, format_exception_message(exc))
        return FilterOptions(unavailable={k: "unreadable" for k in SOURCE_FILTERS.get((section, source), ())})


async def preview_import(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    section: str,
    source: str,
    filters: dict[str, Any],
    batch_type: str = "",
    remove_gone: bool = False,
    excluded_ids: list[str] | None = None,
    sync_id: str = "",
) -> tuple[ImportPlan, int | None]:
    """The import plan, and the profile version it starts from (writes nothing).

    Assets and asset groups are planned against the records (#1543) when the
    organization exists: :func:`apply_import` then writes them row by row. The
    other sections, and a platform with no profile yet, plan on the profile.
    """
    from app.services import organization_context as org_context

    current = await org_context.current_version(db)
    read = await read_source(
        db, user_id=user_id, section=section, source=source, filters=filters, batch_type=batch_type
    )
    if section in RECORD_SECTIONS and current is not None:
        plan = await _plan_on_records(
            db, section, source, read, remove_gone=remove_gone, excluded_ids=excluded_ids, sync_id=sync_id
        )
        if plan is not None:
            return plan, current.version
    base = org_context.parse_profile(current.profile if current else {})
    plan = plan_import(base, section, source, read, remove_gone=remove_gone, excluded_ids=excluded_ids, sync_id=sync_id)
    return plan, (current.version if current else None)


async def _plan_on_records(
    db: AsyncSession,
    section: str,
    source: str,
    read: ReadResult,
    *,
    remove_gone: bool,
    excluded_ids: list[str] | None,
    sync_id: str,
) -> ImportPlan | None:
    """The record plan of an asset or group import (``None``: no organization yet)."""
    from app.services import organization_asset_records as records

    org = await records.ensure_records(db)
    if org is None:
        return None
    refs = [
        (source, str(getattr(r, "external_id", "") or ""))
        for r in read.rows
        if str(getattr(r, "external_id", "") or "")
    ]
    stored = await records.rows_by_ref(db, org, section, refs)
    _attr, name_attr, _cap, _local = SOURCED_FIELDS[section]
    names = await records.name_owners(
        db, org, section, [str(getattr(r, name_attr, "") or "") for r in read.rows], source=source
    )
    gone_known = bool(excluded_ids) or (not read.truncated and not read.error)
    source_rows = (
        [StoredRow(e, n, s) for e, n, s in await records.source_refs(db, org, section, source)] if gone_known else []
    )
    validate = records.validate_asset if section == "assets" else records.validate_group

    def checked(value: dict[str, Any]) -> dict[str, Any]:
        try:
            return validate(value)
        except records.RecordError:
            return value  # the source's own row model validated it; the write reports a bad field

    return plan_record_import(
        section,
        source,
        read,
        validate=checked,
        stored=stored,
        names=names,
        source_rows=source_rows,
        remove_gone=remove_gone,
        excluded_ids=excluded_ids,
        sync_id=sync_id,
        organization_id=org,
    )


async def apply_record_import(db: AsyncSession, plan: ImportPlan, *, actor: str) -> int:
    """Write a record plan (#1543): its rows in batches, then its removals; answers the rows written or removed.

    Origin ``sync:<id>`` for a sync (it owns its rows), else ``import``; each
    changed row is in the change log, its identity cluster re-matched. No
    profile version is saved (the caller commits).
    """
    from app.services import organization_asset_records as records

    if plan.organization_id is None or plan.error:
        return 0
    origin = f"sync:{plan.sync_id}" if plan.sync_id else "import"
    count = 0
    for start in range(0, len(plan.writes), records.MAX_BATCH):
        result = await records.write_rows(
            db,
            plan.organization_id,
            plan.section,
            plan.writes[start : start + records.MAX_BATCH],
            origin=origin,
            actor=actor,
        )
        count += len(result.created) + len(result.updated)
    if plan.remove_refs:
        count += len(
            await records.remove_rows(
                db, plan.organization_id, plan.section, plan.remove_refs, origin=origin, actor=actor
            )
        )
    if plan.section == "asset_groups" and plan.members and plan.members_source:
        # #1556: the source's memberships, after its group rows exist; a hand membership is never touched.
        synced = await records.sync_group_members(
            db,
            plan.organization_id,
            plan.source,
            plan.members,
            member_source=plan.members_source,
            sync_id=plan.sync_id,
        )
        count += synced.added + synced.removed
    return count


def provenance(plan: ImportPlan, filters: dict[str, Any]) -> dict[str, dict[str, str]]:
    """The ``sources`` entry the saved version keeps for the field the import wrote."""
    used = ", ".join(f"{k}={v}" for k, v in sorted(filters.items()) if v not in (None, "", [], {}))
    basis = f"import of {plan.section}: {len(plan.new)} new, {len(plan.updated)} updated"
    if plan.gone:
        removed = len(plan.gone) - len(plan.gone_kept) if plan.remove_gone else 0
        basis += f", {len(plan.gone)} no longer in the source ({removed} removed, {len(plan.gone) - removed} kept)"
    if plan.excluded:
        basis += f", {len(plan.excluded)} excluded by the sync"
    basis += f"; read {datetime.now(UTC).strftime('%Y-%m-%dT%H:%MZ')}"
    if used:
        basis += f"; filters {used}"[:400]
    return {SECTION_FIELD[plan.section]: {"source": plan.source, "basis": basis[:500]}}
