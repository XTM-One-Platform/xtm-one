# SPDX-License-Identifier: Apache-2.0

"""The organization profile: one structured, versioned context every agent reads.

The contract is ``dev-docs/ctem-fleets.md`` section 6 (#8987): sector,
regions, size, crown jewels, tech stack, AI systems inventory, PIRs, risk
appetite, loaded hourly rate and currency, ticketing target. It replaces
the free-text ``company-context`` knowledge base as the source agents
reason from: a stage analyst ranks by the crown jewels it reads here and
names what is missing instead of guessing it.

Storage is ``organization_context_versions``: a save inserts the next
version of one organization, its current profile is the highest one.
Since #9157 every version belongs to an organization
(``models/organization.py``): a reader that names none reads the primary
organization, exactly as before; the tracked organizations and their
smaller profile are ``services/organizations.py``'s. :func:`missing_fields`
is the single answer to "what is not filled", used by the tool, the API
and the fleet runs alike.
"""

from __future__ import annotations

import re
import uuid
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.organization_context import ORGANIZATION_CONTEXT_SOURCES, OrganizationContextVersion

_ISO_COUNTRY = re.compile(r"^[A-Z]{2}$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")

#: Organization size bands (employees).
SIZE_BANDS: tuple[str, ...] = ("1-50", "51-250", "251-1000", "1001-5000", "5001-20000", "20000+")
CRITICALITIES: tuple[str, ...] = ("low", "medium", "high", "critical")
AI_SYSTEM_KINDS: tuple[str, ...] = ("assistant", "agent", "model_api", "embedded")
AI_EXPOSURES: tuple[str, ...] = ("internal", "partner", "public")
#: The asset taxonomy of OpenAEV (``AssetCategory`` / ``AssetSubCategory``, openaev #6452), lower-cased as
#: OpenCRQ stores it: category -> its subcategories. The single source of the category and subcategory
#: an asset row carries (#11781); ``dev-docs/asset-model.md`` maps it across the platforms.
ASSET_TAXONOMY: dict[str, tuple[str, ...]] = {
    "host": ("server", "workstation", "laptop", "virtual_machine", "hypervisor", "mainframe", "thin_client"),
    "container_workload": (
        "container",
        "container_image",
        "kubernetes_pod",
        "kubernetes_cluster",
        "kubernetes_node",
        "serverless_function",
    ),
    "cloud_resource": (
        "compute",
        "storage",
        "database",
        "networking",
        "serverless",
        "container_registry",
        "kubernetes",
        "iam_principal",
        "secrets_key_mgmt",
        "messaging_queue",
        "analytics_data",
        "ai_ml_service",
        "iac_template",
        "cloud_other",
    ),
    "web_application": ("website", "web_api", "single_page_app", "graphql_api", "web_service", "microservice"),
    "network_device": (
        "router",
        "switch",
        "firewall",
        "load_balancer",
        "vpn_gateway",
        "wireless_ap",
        "proxy",
        "dns_server",
        "dhcp_server",
        "san_nas",
        "network_other",
    ),
    "mobile_device": ("smartphone", "tablet"),
    "iot_ot_device": (
        "iot_sensor",
        "ip_camera",
        "gateway",
        "point_of_sale",
        "media_device",
        "plc",
        "rtu",
        "hmi",
        "scada_historian",
        "medical_device",
        "printer_peripheral",
        "building_mgmt",
    ),
    "identity": ("user_account", "service_account", "group", "role", "shared_mailbox", "non_human_identity"),
    "saas_application": ("saas_app", "saas_tenant"),
    "ai_target": ("llm_model", "ai_agent", "mcp_server", "rag_pipeline"),
    "security_platform": (
        "edr",
        "xdr",
        "siem",
        "soar",
        "ndr",
        "ispm",
        "email_security",
        "llm_firewall",
        "ai_gateway",
        "vulnerability_scanner",
    ),
    "generic_asset": (),
}
ASSET_CATEGORIES: tuple[str, ...] = tuple(ASSET_TAXONOMY)
#: subcategory -> its category (every subcategory belongs to exactly one).
ASSET_SUBCATEGORIES: dict[str, str] = {sub: cat for cat, subs in ASSET_TAXONOMY.items() for sub in subs}
#: OpenAEV's ``AssetCriticality``, lower-cased (OpenCRQ's scale too).
ASSET_CRITICALITIES: tuple[str, ...] = ("unknown", "low", "medium", "high", "very_high")
#: The flat ``type`` of #11543, read on an old profile or a CSV: type -> (category, subcategory).
#: ``asset_group`` is not an asset any more: such a row moves to ``asset_groups`` whole.
LEGACY_ASSET_TYPES: dict[str, tuple[str, str]] = {
    "host": ("host", ""),
    "application": ("web_application", ""),
    "cloud_service": ("cloud_resource", ""),
    "database": ("cloud_resource", "database"),
    "network": ("network_device", ""),
    "endpoint": ("host", "workstation"),
    "identity": ("identity", ""),
    "other": ("generic_asset", ""),
}
LEGACY_ASSET_GROUP_TYPE = "asset_group"
ASSET_ENVIRONMENTS: tuple[str, ...] = ("", "production", "staging", "development", "test", "other")
INTERNET_FACING: tuple[str, ...] = ("yes", "no", "unknown")
EXCLUSION_KINDS: tuple[str, ...] = ("product", "asset_group", "tag")
KNOWLEDGE_KINDS: tuple[str, ...] = ("sop", "runbook", "patch_policy", "reference", "other")
#: The profile parts added by #11543 (``asset_groups`` by #11781). They are read by tool, never dumped into
#: the always-on profile block, and they are not contract fields: an existing profile
#: reads, renders and reports its missing fields exactly as before.
EXPOSURE_FIELDS: tuple[str, ...] = ("assets_exposure", "asset_groups", "prioritization_policy", "enterprise_knowledge")
#: Profile fields read by a CTEM page only (#11547 S2e), never dumped into the
#: always-on block and not contract fields, like the parts above.
PAGE_ONLY_FIELDS: tuple[str, ...] = ("posture_target",)

_Strict = ConfigDict(extra="forbid", str_strip_whitespace=True)


def normalize_regions(value: list[str]) -> list[str]:
    """ISO 3166-1 alpha-2 codes, upper-cased and deduplicated (``ValueError`` on anything else)."""
    regions: list[str] = []
    for region in value:
        code = str(region).strip().upper()
        if not _ISO_COUNTRY.match(code):
            raise ValueError(f"region {region!r} is not an ISO 3166-1 alpha-2 code (e.g. FR, US)")
        if code not in regions:
            regions.append(code)
    return regions


class CrownJewel(BaseModel):
    model_config = _Strict

    name: str = Field(min_length=1, max_length=200)
    business_service: str = Field(default="", max_length=200)
    owner: str = Field(default="", max_length=200)
    criticality: Literal["low", "medium", "high", "critical"] = "high"
    opencrq_asset_id: str = Field(default="", max_length=100)
    #: The ``assets_exposure`` rows (assets or asset groups, by name) this crown jewel runs on (#11543).
    asset_names: list[str] = Field(default_factory=list, max_length=200)

    @field_validator("asset_names")
    @classmethod
    def _asset_names(cls, value: list[str]) -> list[str]:
        return [name for name in dict.fromkeys(str(v).strip()[:200] for v in value) if name]


class TechStackEntry(BaseModel):
    model_config = _Strict

    product: str = Field(min_length=1, max_length=200)
    vendor: str = Field(default="", max_length=200)
    cpe: str = Field(default="", max_length=300)
    #: The version the organization runs, optional (CTEM-11): cases then name the version to reach.
    version: str = Field(default="", max_length=100)


class AiSystem(BaseModel):
    model_config = _Strict

    name: str = Field(min_length=1, max_length=200)
    kind: Literal["assistant", "agent", "model_api", "embedded"] = "assistant"
    owner: str = Field(default="", max_length=200)
    exposure: Literal["internal", "partner", "public"] = "internal"
    openaev_asset_id: str = Field(default="", max_length=100)


def _names(value: list[str], *, limit: int = 200) -> list[str]:
    """Stripped, bounded, deduplicated, empty entries dropped (tags, product names)."""
    return [name for name in dict.fromkeys(str(v).strip()[:limit] for v in value) if name]


def upgrade_legacy_type(data: dict[str, Any]) -> dict[str, Any]:
    """*data* with a #11543 ``type`` read as its category and subcategory (``ValueError`` on an unknown one).

    A row that already names a category keeps it; ``type`` is dropped either way.
    """
    if "type" not in data:
        return data
    data = dict(data)
    legacy = str(data.pop("type") or "").strip().lower()
    if data.get("category"):
        return data
    if legacy == LEGACY_ASSET_GROUP_TYPE:
        raise ValueError("an asset group is not an asset: it goes in asset_groups")
    if legacy in ASSET_TAXONOMY:
        data["category"] = legacy
        return data
    if legacy in ASSET_SUBCATEGORIES:
        data["category"], data["subcategory"] = ASSET_SUBCATEGORIES[legacy], legacy
        return data
    if legacy not in LEGACY_ASSET_TYPES and legacy:
        raise ValueError(f"type {legacy!r} is not an asset type or category")
    category, subcategory = LEGACY_ASSET_TYPES.get(legacy or "host", ("host", ""))
    data["category"] = category
    if subcategory and not data.get("subcategory"):
        data["subcategory"] = subcategory
    return data


class AssetEntry(BaseModel):
    """One asset of the attack surface (#11543), typed on OpenAEV's taxonomy (#11781).

    ``category`` and ``subcategory`` are OpenAEV's ``asset_category`` and
    ``asset_subcategory`` lower-cased (:data:`ASSET_TAXONOMY`); ``criticality``
    is its ``asset_criticality``; ``internet_facing`` maps to
    ``asset_internet_facing`` (yes true, no false, unknown null). A row saved
    with the flat ``type`` of #11543 is read through :func:`upgrade_legacy_type`.
    ``source`` and ``external_id`` say where a row came from (``manual``,
    ``csv``, ``api``, later an OpenCTI or OpenAEV asset source) so a sync can
    update its own rows without touching the ones a person typed.
    """

    model_config = _Strict

    name: str = Field(min_length=1, max_length=200)
    category: Literal[
        "host",
        "container_workload",
        "cloud_resource",
        "web_application",
        "network_device",
        "mobile_device",
        "iot_ot_device",
        "identity",
        "saas_application",
        "ai_target",
        "security_platform",
        "generic_asset",
    ] = "generic_asset"
    #: A subcategory of ``category`` (empty: none chosen).
    subcategory: str = Field(default="", max_length=50)
    criticality: Literal["unknown", "low", "medium", "high", "very_high"] = "unknown"
    environment: Literal["", "production", "staging", "development", "test", "other"] = ""
    internet_facing: Literal["yes", "no", "unknown"] = "unknown"
    owner: str = Field(default="", max_length=200)
    #: The ``asset_groups`` entry this asset belongs to, by name (empty: none).
    group: str = Field(default="", max_length=200)
    #: The ``tech_stack`` products running on it, by product name.
    tech_stack: list[str] = Field(default_factory=list, max_length=100)
    tags: list[str] = Field(default_factory=list, max_length=50)
    source: str = Field(default="manual", max_length=50)
    external_id: str = Field(default="", max_length=200)

    @model_validator(mode="before")
    @classmethod
    def _legacy_type(cls, data: Any) -> Any:
        return upgrade_legacy_type(data) if isinstance(data, dict) else data

    @field_validator("tech_stack", "tags")
    @classmethod
    def _clean_names(cls, value: list[str]) -> list[str]:
        return _names(value)

    @model_validator(mode="after")
    def _subcategory_of_category(self) -> AssetEntry:
        sub = self.subcategory.strip().lower()
        if sub and ASSET_SUBCATEGORIES.get(sub) != self.category:
            raise ValueError(f"subcategory {sub!r} is not a subcategory of {self.category}")
        self.subcategory = sub
        return self


class AssetGroupEntry(BaseModel):
    """A group of assets (OpenAEV's static asset group, #11781): assets join it through their ``group``.

    The fields an ``asset_group`` row of #11543 carried are kept, so the move
    from ``assets_exposure`` loses nothing; a nested ``group`` (OpenAEV has no
    nested groups) is kept as the tag ``group:<name>``.
    """

    model_config = _Strict

    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=1000)
    environment: Literal["", "production", "staging", "development", "test", "other"] = ""
    internet_facing: Literal["yes", "no", "unknown"] = "unknown"
    owner: str = Field(default="", max_length=200)
    tech_stack: list[str] = Field(default_factory=list, max_length=100)
    tags: list[str] = Field(default_factory=list, max_length=50)
    source: str = Field(default="manual", max_length=50)
    external_id: str = Field(default="", max_length=200)

    @field_validator("tech_stack", "tags")
    @classmethod
    def _clean_names(cls, value: list[str]) -> list[str]:
        return _names(value)


def legacy_group_row(row: dict[str, Any]) -> dict[str, Any]:
    """An ``asset_group`` row of #11543 as an :class:`AssetGroupEntry` value, every field kept."""
    group = {key: value for key, value in row.items() if key not in ("type", "group", "category", "subcategory")}
    nested = str(row.get("group") or "").strip()
    if nested:
        group["tags"] = [*(group.get("tags") or []), f"group:{nested}"]
    return group


def _is_legacy_group(row: Any) -> bool:
    return isinstance(row, dict) and str(row.get("type") or "").strip().lower() == LEGACY_ASSET_GROUP_TYPE


class PolicyExclusion(BaseModel):
    """A finding the funnel leaves out: on a product, an asset group or a tag."""

    model_config = _Strict

    kind: Literal["product", "asset_group", "tag"] = "product"
    value: str = Field(min_length=1, max_length=200)
    reason: str = Field(default="", max_length=500)


class AcceptedRisk(BaseModel):
    """A risk the organization accepts until a date, with the person who owns the decision."""

    model_config = _Strict

    #: What is accepted: a CVE id, a product, a finding title.
    subject: str = Field(min_length=1, max_length=300)
    #: Where: an asset or asset group name (empty: everywhere).
    scope: str = Field(default="", max_length=200)
    owner: str = Field(min_length=1, max_length=200)
    expires_on: date
    reason: str = Field(default="", max_length=1000)


class SeveritySla(BaseModel):
    """Hours to fix a CTEM case of each severity (#847): P1 a day, P2 three days, P3 thirty days."""

    model_config = _Strict

    p1: int = Field(default=24, ge=1, le=24 * 365)
    p2: int = Field(default=72, ge=1, le=24 * 365)
    p3: int = Field(default=720, ge=1, le=24 * 365)


class PrioritizationPolicy(BaseModel):
    """The explicit rules the CTEM prioritization funnel and its agents apply (#11543).

    Every threshold is off by default, so a profile that never set a policy
    ranks exactly as before; :data:`RECOMMENDED_POLICY` is the starting point
    the settings page offers. Money thresholds stay in ``risk_appetite``.
    """

    model_config = _Strict

    cvss_min: float | None = Field(default=None, ge=0, le=10)
    epss_min: float | None = Field(default=None, ge=0, le=1)
    #: A vulnerability in CISA KEV passes the thresholds whatever its scores.
    kev_always_in: bool = True
    #: Keep only findings on an asset whose ``internet_facing`` is ``yes``: an opt-in that also drops the
    #: findings on user endpoints, which the Exposed layer always keeps (``asset_reachability``).
    internet_exposure_required: bool = False
    #: Keep only findings on assets linked to a crown jewel of at least this criticality (empty: any).
    min_crown_jewel_criticality: Literal["", "low", "medium", "high", "critical"] = ""
    exclusions: list[PolicyExclusion] = Field(default_factory=list, max_length=200)
    accepted_risks: list[AcceptedRisk] = Field(default_factory=list, max_length=500)
    #: CTEM scope gate (#847): a confirmed signal opens a case when its subject's criticality (assets,
    #: crown jewels, risk scenarios) is at least this; below, it goes to the watchlist.
    scope_min_criticality: Literal["low", "medium", "high", "critical"] = "high"
    #: CTEM scope gate (#847): a case also needs an OpenCRQ risk scenario holding the subject.
    require_risk_scenario: bool = False
    #: CTEM scope gate (#847): the time to fix a case of each severity, in hours (the case's ``sla_due_at``).
    sla_hours: SeveritySla = Field(default_factory=SeveritySla)
    #: CTEM trigger rules (CTEM-08): the PIR score (0 to 100) an OpenCTI entity flagged in a profile PIR
    #: must reach before a fleet run starts on it: a vulnerability, and threat activity (an intrusion
    #: set, a campaign, a malware...). Read live from OpenCTI at delivery, never the delivered score.
    pir_min_score_vulnerability: int = Field(default=70, ge=0, le=100)
    pir_min_score_threat: int = Field(default=75, ge=0, le=100)


#: The values the settings page proposes for a first policy (never applied unless saved).
RECOMMENDED_POLICY: dict[str, Any] = {
    "cvss_min": 7.0,
    "epss_min": 0.1,
    "kev_always_in": True,
    "internet_exposure_required": False,
    "min_crown_jewel_criticality": "",
}


def policy_is_set(policy: PrioritizationPolicy) -> bool:
    """Whether *policy* says anything beyond the defaults (the category's fill state)."""
    return policy != PrioritizationPolicy()


class KnowledgeLink(BaseModel):
    """A knowledge base holding the organization's own procedures (SOPs, runbooks, patch policy)."""

    model_config = _Strict

    knowledge_base_id: str = Field(min_length=1, max_length=64)
    #: The name shown when the reader cannot see the knowledge base itself.
    label: str = Field(default="", max_length=200)
    kind: Literal["sop", "runbook", "patch_policy", "reference", "other"] = "reference"


class RiskAppetite(BaseModel):
    model_config = _Strict

    annual_loss_threshold: float | None = Field(default=None, ge=0)
    approval_threshold: float | None = Field(default=None, ge=0)


class TicketingTarget(BaseModel):
    model_config = _Strict

    integration: str = Field(default="", max_length=100)
    project: str = Field(default="", max_length=200)


class OrganizationProfile(BaseModel):
    """The organization profile as stored and served (every field optional)."""

    model_config = _Strict

    name: str = Field(default="", max_length=200)
    sector: str = Field(default="", max_length=100)
    sub_sector: str = Field(default="", max_length=100)
    regions: list[str] = Field(default_factory=list, max_length=250)
    size: Literal["", "1-50", "51-250", "251-1000", "1001-5000", "5001-20000", "20000+"] = ""
    crown_jewels: list[CrownJewel] = Field(default_factory=list, max_length=200)
    tech_stack: list[TechStackEntry] = Field(default_factory=list, max_length=500)
    ai_systems: list[AiSystem] = Field(default_factory=list, max_length=200)
    pir_ids: list[str] = Field(default_factory=list, max_length=100)
    #: The last known OpenCTI name of each PIR in ``pir_ids``, saved with it so the
    #: profile names a PIR even when OpenCTI cannot be read or deleted it (#9615).
    #: Not a contract field: ``missing_fields`` ignores it.
    pir_names: dict[str, str] = Field(default_factory=dict)
    risk_appetite: RiskAppetite = Field(default_factory=RiskAppetite)
    loaded_hourly_rate: float | None = Field(default=None, ge=0)
    currency: str = Field(default="", max_length=3)
    ticketing: TicketingTarget = Field(default_factory=TicketingTarget)
    #: Assets of the attack surface, with their exposure (#11543).
    assets_exposure: list[AssetEntry] = Field(default_factory=list, max_length=5000)
    #: Groups the assets join through their ``group`` (#11781; ``asset_group`` rows of #11543 move here).
    asset_groups: list[AssetGroupEntry] = Field(default_factory=list, max_length=500)
    #: The rules the prioritization funnel applies (#11543); defaults change nothing.
    prioritization_policy: PrioritizationPolicy = Field(default_factory=PrioritizationPolicy)
    #: Knowledge bases of the organization's procedures, read by tool only (#11543).
    enterprise_knowledge: list[KnowledgeLink] = Field(default_factory=list, max_length=50)
    #: The Exposure Posture score (0-100) the organization aims for, drawn as the
    #: target line of the CTEM Governance & Risk page (#11547 S2e). Empty: no target,
    #: the page asks for one. Not a contract field and never preloaded.
    posture_target: int | None = Field(default=None, ge=0, le=100)
    #: The contract fields the person chose to skip in the chat onboarding
    #: (``services/organization_onboarding.py``, #11224). Not a contract field:
    #: ``missing_fields`` still reports them; the onboarding no longer asks.
    onboarding_skipped: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="before")
    @classmethod
    def _asset_groups_out_of_assets(cls, data: Any) -> Any:
        """A profile saved before #11781: its ``asset_group`` rows move to ``asset_groups``, whole."""
        if not isinstance(data, dict) or not isinstance(data.get("assets_exposure"), list):
            return data
        rows = data["assets_exposure"]
        if not any(_is_legacy_group(row) for row in rows):
            return data
        data = dict(data)
        groups = list(data.get("asset_groups") or [])
        known = {str(g.get("name") or "").strip().lower() for g in groups if isinstance(g, dict)}
        for row in rows:
            if _is_legacy_group(row) and str(row.get("name") or "").strip().lower() not in known:
                groups.append(legacy_group_row(row))
                known.add(str(row.get("name") or "").strip().lower())
        data["assets_exposure"] = [row for row in rows if not _is_legacy_group(row)]
        data["asset_groups"] = groups
        return data

    @field_validator("onboarding_skipped")
    @classmethod
    def _skipped_paths(cls, value: list[str]) -> list[str]:
        paths = {path for path, _label in CONTRACT_FIELDS}
        return [path for path in dict.fromkeys(str(v).strip() for v in value) if path in paths]

    @field_validator("regions")
    @classmethod
    def _iso_regions(cls, value: list[str]) -> list[str]:
        return normalize_regions(value)

    @field_validator("currency")
    @classmethod
    def _iso_currency(cls, value: str) -> str:
        code = value.strip().upper()
        if code and not _CURRENCY.match(code):
            raise ValueError(f"currency {value!r} is not an ISO 4217 code (e.g. EUR, USD)")
        return code

    @field_validator("pir_ids")
    @classmethod
    def _pir_ids(cls, value: list[str]) -> list[str]:
        return [pir for pir in dict.fromkeys(str(v).strip() for v in value) if pir]

    @model_validator(mode="after")
    def _names_of_saved_pirs(self) -> OrganizationProfile:
        # Only the PIRs the profile follows keep a name, each bounded like a name field.
        names = {str(k).strip(): str(v).strip()[:300] for k, v in self.pir_names.items()}
        self.pir_names = {pir: names[pir] for pir in self.pir_ids if names.get(pir)}
        return self


#: Every field of the contract, in the order a report names them, with the
#: words a person reads when it is missing.
CONTRACT_FIELDS: tuple[tuple[str, str], ...] = (
    ("name", "organization name"),
    ("sector", "sector"),
    ("regions", "regions of operation"),
    ("size", "organization size"),
    ("crown_jewels", "crown jewels and business services"),
    ("tech_stack", "tech stack"),
    ("ai_systems", "AI systems inventory"),
    ("pir_ids", "priority intelligence requirements (PIRs)"),
    ("risk_appetite.annual_loss_threshold", "risk appetite: annual loss threshold"),
    ("risk_appetite.approval_threshold", "risk appetite: approval threshold"),
    ("loaded_hourly_rate", "loaded hourly rate"),
    ("currency", "currency"),
    ("ticketing", "ticketing target"),
)


def _value(profile: OrganizationProfile, path: str) -> Any:
    value: Any = profile
    for part in path.split("."):
        value = getattr(value, part)
    return value


def missing_fields(profile: OrganizationProfile) -> list[str]:
    """The contract fields *profile* leaves empty, by path, in contract order."""
    missing: list[str] = []
    for path, _label in CONTRACT_FIELDS:
        value = _value(profile, path)
        if path == "ticketing":
            empty = not (value.integration and value.project)
        else:
            empty = value is None or value == "" or value == []
        if empty:
            missing.append(path)
    return missing


#: The info line every brief, report or assessment an agent writes carries
#: while the profile is incomplete (#11586): never dismissible, the fields not
#: filled named. ``{fields}`` is the labels joined; bounded by the contract.
BRIEF_NOTICE_LINE = "Organization context incomplete: missing {fields}. The analysis would be sharper with them."


def brief_notice_instruction(missing: list[str]) -> str:
    """What the agent is told to add to its briefs when *missing* (contract paths) names a field.

    ``""`` for a complete profile: a brief then carries no notice. Implemented
    once here and read by every profile block an agent gets (the basic
    summary of every turn and the ``get_organization_context`` reading).
    """
    if not missing:
        return ""
    labels = dict(CONTRACT_FIELDS)
    line = BRIEF_NOTICE_LINE.format(fields=", ".join(labels.get(path, path) for path in missing))
    return f'In any brief, report or assessment you write, add this info line (not a warning): "{line}"'


def parse_profile(raw: Any) -> OrganizationProfile:
    """A stored or submitted profile, validated (``ValueError`` on a bad field)."""
    from pydantic import ValidationError

    try:
        return OrganizationProfile.model_validate(raw or {})
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(part) for part in first.get("loc", ())) or "profile"
        raise ValueError(f"{where}: {first.get('msg', 'invalid value')}") from None


def accepted_sources(raw: dict[str, Any] | None) -> dict[str, dict[str, str]] | None:
    """The provenance an ``ai_draft`` save keeps: contract fields only, ``source`` and ``basis`` bounded."""
    paths = {path for path, _label in CONTRACT_FIELDS}
    kept: dict[str, dict[str, str]] = {}
    for path, entry in (raw or {}).items():
        if path not in paths or not isinstance(entry, dict):
            continue
        kept[path] = {
            "source": str(entry.get("source") or "")[:300],
            "basis": str(entry.get("basis") or "")[:500],
        }
    return kept or None


def _of_organization(organization_id: uuid.UUID | None) -> ColumnElement[bool]:
    """The ``WHERE`` that keeps one organization's versions (the primary's by default)."""
    from app.models.organization import Organization

    if organization_id is not None:
        return OrganizationContextVersion.organization_id == organization_id
    primary = select(Organization.id).where(Organization.role == "primary").scalar_subquery()
    return OrganizationContextVersion.organization_id == primary


async def current_version(
    db: AsyncSession, organization_id: uuid.UUID | None = None
) -> OrganizationContextVersion | None:
    """The latest saved version of *organization_id* (the primary when ``None``), or ``None``."""
    return (
        await db.execute(
            select(OrganizationContextVersion)
            .where(_of_organization(organization_id))
            .order_by(OrganizationContextVersion.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def list_versions(
    db: AsyncSession, *, limit: int = 20, organization_id: uuid.UUID | None = None
) -> list[OrganizationContextVersion]:
    """The latest *limit* versions of *organization_id* (the primary when ``None``), newest first."""
    return list(
        (
            await db.execute(
                select(OrganizationContextVersion)
                .where(_of_organization(organization_id))
                .order_by(OrganizationContextVersion.version.desc())
                .limit(max(1, min(limit, 100)))
            )
        )
        .scalars()
        .all()
    )


async def save_version(
    db: AsyncSession,
    profile: BaseModel,
    *,
    source: str,
    author_user_id: uuid.UUID | None,
    organization_id: uuid.UUID | None = None,
    sources: dict[str, Any] | None = None,
) -> OrganizationContextVersion:
    """Insert *profile* as the next version of *organization_id* (the caller commits).

    ``None`` saves the primary organization's profile, creating the primary
    on a platform that has none yet (a fresh install). *profile* is any
    validated profile model: the primary's :class:`OrganizationProfile` or a
    tracked organization's (``services/organizations.py``). Two saves racing
    for the same number: the unique ``(organization_id, version)`` index
    refuses the second, which the API answers 409. *sources* is the
    provenance an ``ai_draft`` save keeps (field -> ``{source, basis}``).
    """
    if source not in ORGANIZATION_CONTEXT_SOURCES:
        raise ValueError(f"source must be one of {', '.join(ORGANIZATION_CONTEXT_SOURCES)}")
    if organization_id is None:
        from app.services.organizations import ensure_primary

        organization_id = (await ensure_primary(db, name=getattr(profile, "name", ""))).id
    latest = (
        await db.execute(
            select(func.max(OrganizationContextVersion.version)).where(
                OrganizationContextVersion.organization_id == organization_id
            )
        )
    ).scalar()
    row = OrganizationContextVersion(
        organization_id=organization_id,
        version=int(latest or 0) + 1,
        profile=profile.model_dump(mode="json"),
        source=source,
        sources=sources or None,
        author_user_id=author_user_id,
    )
    db.add(row)
    await db.flush()
    return row


#: Header of the block a fleet run's prompt carries (see :func:`fleet_run_context`).
FLEET_RUN_CONTEXT_HEADER = "--- Organization context ---"


async def fleet_run_context(
    db: AsyncSession,
    agent_id: object,
    *,
    fleet: object | None = None,
    organization_ids: list[uuid.UUID] | None = None,
) -> str:
    """The organization profile block appended to a fleet member's run prompt.

    A run of an agent whose home fleet exists starts from the profile instead
    of discovering it (or not) through ``get_organization_context``: the
    filled fields quarantined, the missing ones named. An agent in no fleet
    gets ``""`` (it still has the tool). A failed read never fails the run.
    *fleet* (a ``FleetRunDefaults`` the caller resolved, e.g.
    ``fleet_runs.assignment_fleet_context``) skips the home-fleet read.
    *organization_ids* are the tracked organizations the triggering event
    concerns (``services/organization_events.py``, #9157 M4): their profiles
    follow the primary's, so the run names the organization.
    """
    import logging

    from app.core.utils import format_exception_message
    from app.services.fleet_runs import home_fleet_defaults

    try:
        resolved = fleet if fleet is not None else await home_fleet_defaults(db, agent_id)
        if getattr(resolved, "fleet_id", None) is None:
            return ""
        block = render_for_agent(await current_version(db))
        tracked = await scoped_tracked_blocks(db, getattr(resolved, "organization_scope", None))
        scoped = f"\n\n{FLEET_RUN_CONTEXT_HEADER}\n" + "\n\n".join([block, *tracked])
        return scoped + await _concerned_organizations_block(db, organization_ids or [])
    except Exception as exc:  # the profile is context, never a reason to fail a run
        logging.getLogger(__name__).warning(
            "Organization context unreadable for agent %s: %s", agent_id, format_exception_message(exc)
        )
        return ""


async def _concerned_organizations_block(db: AsyncSession, organization_ids: list[uuid.UUID]) -> str:
    """The profiles of the tracked organizations an event concerns, after the primary's (``""`` when none)."""
    from app.services import organizations as orgs

    parts: list[str] = []
    for organization_id in organization_ids:
        organization = await orgs.get_organization(db, organization_id)
        if organization is None or organization.role != "tracked" or organization.archived_at is not None:
            continue
        parts.append(orgs.render_tracked_for_agent(organization, await current_version(db, organization.id)))
    if not parts:
        return ""
    return "\n\nThe event that started this run concerns this tracked organization:\n" + "\n\n".join(parts)


async def scoped_organizations(db: AsyncSession, scope: dict | None) -> list[Any]:
    """The tracked organizations a member's scope names, active only, by name.

    ``primary`` (or no scope) names none: the primary is always read. ``monitored``
    names every tracked organization whose monitoring is on; ``list`` the ones
    it lists that are still tracked and not archived (dev-docs/ctem-fleets.md 10.3).
    """
    from app.models.organization import Organization

    mode = (scope or {}).get("mode") if isinstance(scope, dict) else None
    if mode not in ("monitored", "list"):
        return []
    query = select(Organization).where(Organization.role == "tracked", Organization.archived_at.is_(None))
    if mode == "monitored":
        query = query.where(Organization.monitoring == "on")
    else:
        ids: list[uuid.UUID] = []
        for raw in scope.get("organization_ids") or []:  # type: ignore[union-attr]
            try:
                ids.append(uuid.UUID(str(raw)))
            except (TypeError, ValueError):
                continue
        if not ids:
            return []
        query = query.where(Organization.id.in_(ids))
    rows = list((await db.execute(query)).scalars().all())
    return sorted(rows, key=lambda o: o.name.lower())


async def scoped_tracked_blocks(db: AsyncSession, scope: dict | None) -> list[str]:
    """One block per tracked organization of *scope*, after the primary's."""
    from app.services.organizations import render_tracked_for_agent

    return [
        render_tracked_for_agent(organization, await current_version(db, organization.id))
        for organization in await scoped_organizations(db, scope)
    ]


def legacy_dump(profile: OrganizationProfile) -> dict[str, Any]:
    """The profile as dumped before #11543: the parts it added left out, an empty crown jewel link too.

    An empty tech stack version (CTEM-11) is left out the same way.

    The always-on block and every reader that dumped the whole profile keep
    their exact text for a profile that never filled the new parts.
    """
    dumped = profile.model_dump(mode="json", exclude=set(EXPOSURE_FIELDS) | set(PAGE_ONLY_FIELDS))
    for jewel in dumped.get("crown_jewels") or []:
        if not jewel.get("asset_names"):
            jewel.pop("asset_names", None)
    for entry in dumped.get("tech_stack") or []:
        if not entry.get("version"):
            entry.pop("version", None)
    return dumped


def exposure_counts(profile: OrganizationProfile) -> dict[str, int]:
    """The size of each part #11543 added (the policy counts its explicit rules)."""
    policy = profile.prioritization_policy
    defaults = PrioritizationPolicy()
    rules = sum(
        1
        for name in PrioritizationPolicy.model_fields
        if name not in ("exclusions", "accepted_risks") and getattr(policy, name) != getattr(defaults, name)
    )
    return {
        "assets_exposure": len(profile.assets_exposure),
        "asset_groups": len(profile.asset_groups),
        "prioritization_policy": rules + len(policy.exclusions) + len(policy.accepted_risks),
        "enterprise_knowledge": len(profile.enterprise_knowledge),
    }


def exposure_line(profile: OrganizationProfile) -> str:
    """One announcement line for the filled parts of #11543 (``""`` when none is): never their content."""
    counts = {name: count for name, count in exposure_counts(profile).items() if count}
    if not counts:
        return ""
    parts = ", ".join(f"{name} ({count})" for name, count in counts.items())
    return f"Also filled, read by tool: {parts}; read one with get_organization_context(category=NAME)."


def render_for_agent(row: OrganizationContextVersion | None) -> str:
    """The profile as an agent reads it: the filled fields, then the missing ones.

    The missing fields are named so the run reports them instead of
    guessing; the profile is data a person typed, so it is quarantined.
    """
    import json

    from app.services.content_safety import wrap_untrusted

    if row is None:
        return (
            "No organization profile is saved yet. Every field is missing: "
            + ", ".join(label for _path, label in CONTRACT_FIELDS)
            + ". Do not guess them: name the fields your work needed in your output, "
            "and say an administrator fills them in Settings > Organization. "
            + brief_notice_instruction([path for path, _label in CONTRACT_FIELDS])
        )
    profile = parse_profile(row.profile)
    missing = missing_fields(profile)
    filled = {
        key: value
        for key, value in legacy_dump(profile).items()
        if value not in ("", [], None)
        and not (isinstance(value, dict) and not any(value.values()))
        and key != "onboarding_skipped"
    }
    labels = dict(CONTRACT_FIELDS)
    exposure = exposure_line(profile)
    lines = [
        f"Organization profile, version {row.version} (saved {row.created_at:%Y-%m-%d %H:%M} UTC, source {row.source}).",
        "Cite the fields you used. Fields not filled: "
        + (", ".join(labels[path] for path in missing) if missing else "none")
        + "."
        + (" Do not guess them: name the ones your work needed in your output." if missing else "")
        + (f" {brief_notice_instruction(missing)}" if missing else ""),
        "",
        wrap_untrusted(json.dumps(filled, ensure_ascii=False, indent=1), "organization profile"),
    ]
    if exposure:
        lines.append(exposure)
    return "\n".join(lines)
