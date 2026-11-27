# SPDX-License-Identifier: Apache-2.0

"""The organization profile: one structured, versioned context every agent reads.

The contract is ``dev-docs/ctem-fleets.md`` section 6 (#8987): sector,
regions, size, primary assets, software, PIRs, risk
appetite, loaded hourly rate and currency, ticketing target. It replaces
the free-text ``company-context`` knowledge base as the source agents
reason from: a stage analyst ranks by the primary assets it reads here and
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

import asyncio
import re
import uuid
from datetime import date
from typing import Annotated, Any, Literal

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
    #: What the organization builds and ships (#1233): a third-party dependency is not an asset, it is
    #: the software (SBOM) of the repository; container images, registries and IaC templates stay where
    #: they are. An OpenAEV or OpenCTI without the value receives ``generic_asset`` (asset-model.md).
    "source_code": ("code_repository", "ci_cd_pipeline", "package_registry", "software_package", "code_other"),
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
EXPOSURE_FIELDS: tuple[str, ...] = (
    "assets_exposure",
    "asset_groups",
    "prioritization_policy",
    "enterprise_knowledge",
    # CTEM-29: read by the scope gate, never dumped into the always-on block.
    "owned_domains",
    # CTEM-26: read by the attack surface pass, never dumped into the always-on block.
    "address_ranges",
    "attack_surface",
    # CTEM-28: read by the evidence health pass, never dumped into the always-on block.
    "evidence_health",
    # Cases recompute interval (#1216): read by the scheduled cases pass, never dumped into the always-on block.
    "cases_recompute",
    # Asset identity and field precedence (#1178): read by the asset store, never dumped into the always-on block.
    "asset_identity",
    # Cases attention weights (#1176): read by the Cases list and case page, never dumped into the always-on block.
    "case_attention",
    # Per path source exclusions (#1445): read where a signal is recorded, never dumped into the always-on block.
    "signal_sources",
)
#: Profile fields read by a CTEM page only (#11547 S2e), never dumped into the
#: always-on block and not contract fields, like the parts above.
PAGE_ONLY_FIELDS: tuple[str, ...] = ("posture_target",)

_Strict = ConfigDict(extra="forbid", str_strip_whitespace=True)
#: The most assets the profile model holds; the records hold more (#1543), the readers get this many.
PROFILE_ASSET_CAP = 5000


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


#: The sources of rows a person wrote (typed, a CSV upload, the REST API): never a platform's rows.
HAND_SOURCES: frozenset[str] = frozenset({"manual", "csv", "api"})
#: The platforms a sync reads; their rows are read-only in the form (#1134).
PLATFORM_SOURCES: frozenset[str] = frozenset({"opencti", "openaev", "opencrq"})
#: The prefix of the source of rows an agent wrote: ``agent:<agent id>``.
AGENT_SOURCE_PREFIX = "agent:"


def is_platform_source(source: str) -> bool:
    """Whether a row of *source* belongs to a platform (a sync owns it) rather than to a person or an agent."""
    source = (source or "").strip().lower()
    return bool(source) and source not in HAND_SOURCES and not source.startswith(AGENT_SOURCE_PREFIX)


def _legacy_platform_id(data: Any, legacy_key: str, platform: str) -> Any:
    """A row saved before #1134 with only *legacy_key* reads as a row of *platform* keyed on that id.

    Only a row with no ``source`` at all is such a row: a row a person typed
    (``manual``, ``csv``, ``api``) or an agent wrote keeps its source and
    carries *legacy_key* as a link only, so a link set by the OpenCRQ
    reconcile never hands a typed row to a sync that may remove it (#1473).
    """
    if not isinstance(data, dict):
        return data
    legacy = str(data.get(legacy_key) or "").strip()
    if not legacy or str(data.get("external_id") or "").strip():
        return data
    if str(data.get("source") or "").strip():
        return data
    data = dict(data)
    data["external_id"] = legacy
    data["source"] = platform
    return data


class PrimaryAsset(BaseModel):
    """A primary asset; ``source`` / ``external_id`` / ``sync_id`` say where it came from (#1134).

    ``opencrq_asset_id`` is the id before #1134, kept readable for one release:
    a row carrying it with no ``source`` reads as an OpenCRQ row, an OpenCRQ
    row keeps it equal to ``external_id``, and on a typed row it is the link
    the OpenCRQ reconcile set, never a change of owner (#1473).
    """

    model_config = _Strict

    name: str = Field(min_length=1, max_length=200)
    business_service: str = Field(default="", max_length=200)
    owner: str = Field(default="", max_length=200)
    criticality: Literal["low", "medium", "high", "critical"] = "high"
    opencrq_asset_id: str = Field(default="", max_length=100)
    #: The ``assets_exposure`` rows (assets or asset groups, by name) this primary asset runs on (#11543).
    asset_names: list[str] = Field(default_factory=list, max_length=200)
    source: str = Field(default="manual", max_length=80)
    external_id: str = Field(default="", max_length=200)
    #: The sync that last wrote the row (empty: typed, a one-off import or an agent).
    sync_id: str = Field(default="", max_length=64)

    @model_validator(mode="before")
    @classmethod
    def _legacy_opencrq(cls, data: Any) -> Any:
        return _legacy_platform_id(data, "opencrq_asset_id", "opencrq")

    @field_validator("asset_names")
    @classmethod
    def _asset_names(cls, value: list[str]) -> list[str]:
        return [name for name in dict.fromkeys(str(v).strip()[:200] for v in value) if name]

    @model_validator(mode="after")
    def _opencrq_id_follows(self) -> PrimaryAsset:
        self.source = self.source.strip().lower() or "manual"
        if self.source == "opencrq" and self.external_id:
            self.opencrq_asset_id = self.external_id[:100]
        return self


class SoftwareEntry(BaseModel):
    model_config = _Strict

    product: str = Field(min_length=1, max_length=200)
    vendor: str = Field(default="", max_length=200)
    cpe: str = Field(default="", max_length=300)
    #: The version the organization runs, optional (CTEM-11): cases then name the version to reach.
    version: str = Field(default="", max_length=100)
    #: Where the row came from (#1134): ``manual``, ``csv``, ``api``, ``agent:<id>`` or a platform.
    source: str = Field(default="manual", max_length=80)
    external_id: str = Field(default="", max_length=200)
    sync_id: str = Field(default="", max_length=64)


class ShippedProduct(BaseModel):
    """A product the organization ships (#1432), apart from the software it runs.

    ``repositories`` are the ``owner/name`` of its source repositories: a
    vulnerability on the product (matched by CPE, then by product and vendor)
    resolves to them in the CTEM scope gate, so it joins the case of the
    repository advisory for the same CVE.
    """

    model_config = _Strict

    product: str = Field(min_length=1, max_length=200)
    vendor: str = Field(default="", max_length=200)
    cpe: str = Field(default="", max_length=300)
    repositories: list[str] = Field(default_factory=list, max_length=50)

    @field_validator("product", "vendor", "cpe")
    @classmethod
    def _stripped(cls, value: str) -> str:
        return value.strip()

    @field_validator("repositories")
    @classmethod
    def _repositories(cls, value: list[str]) -> list[str]:
        """``owner/name`` each once; a GitHub URL is read as its ``owner/name``."""
        out: list[str] = []
        for raw in value:
            text = str(raw or "").strip().removesuffix(".git").strip("/")
            for prefix in ("https://github.com/", "http://github.com/", "github.com/"):
                if text.lower().startswith(prefix):
                    text = text[len(prefix) :]
            if not text:
                continue
            parts = text.split("/")
            if len(parts) != 2 or not all(parts) or len(text) > 200:
                raise ValueError(f"repository {raw!r} is not owner/name")
            out.append(text)
        return list(dict.fromkeys(out))


#: OpenAEV's ``Asset.AI_TARGET_PROVIDER``, lower-cased (#1501).
AI_TARGET_PROVIDERS: tuple[str, ...] = (
    "openai_compatible",
    "anthropic",
    "azure_openai",
    "aws_bedrock",
    "google_vertex",
    "huggingface",
    "ollama",
    "custom_http",
    "mcp_server",
    "agent_http",
    "xtm_one",
)


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
        "source_code",
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
    #: The software products running on it, by product name (the ``software`` rows it runs).
    products: list[str] = Field(default_factory=list, max_length=100)
    tags: list[str] = Field(default_factory=list, max_length=50)
    source: str = Field(default="manual", max_length=80)
    external_id: str = Field(default="", max_length=200)
    #: The sync that last wrote the row (#1134; empty: typed, a one-off import or an agent).
    sync_id: str = Field(default="", max_length=64)
    #: Identity keys the asset store matches records on (#1251; ``organization_asset_identity``):
    #: an endpoint manager or EDR source fills them, a typed row may leave them empty.
    serial: str = Field(default="", max_length=200)
    #: The Entra (Azure AD) device id, the key Intune and Defender share.
    device_id: str = Field(default="", max_length=200)
    hostname: str = Field(default="", max_length=255)
    fqdn: str = Field(default="", max_length=255)
    mac: list[str] = Field(default_factory=list, max_length=10)
    ip: list[str] = Field(default_factory=list, max_length=10)
    #: Operating system as the source reports it (field-level provenance in the store).
    os_family: str = Field(default="", max_length=80)
    os_version: str = Field(default="", max_length=80)
    os_build: str = Field(default="", max_length=80)
    #: A cloud or platform resource id, a strong identity key: a code repository's ``<host>:repository:<id>``
    #: (GitHub and GitLab sources), stable across a rename or a transfer.
    cloud_resource_id: str = Field(default="", max_length=300)
    #: Where the asset is reached or browsed (a repository's web URL), http(s) only. A code repository
    #: row is matched across sources on it (identity kind ``repository``), else on a name shaped ``owner/repo``.
    url: str = Field(default="", max_length=500)
    #: When the source last observed the asset, ISO 8601 UTC (#1134 S4, CTEM-27: OpenAEV's
    #: ``asset_last_seen``; #1251: Intune and Defender's last sync); empty when the source does
    #: not say. Owned by the source.
    last_seen: str = Field(default="", max_length=40)
    #: The source's activity status (OpenAEV ``asset_status`` lower-cased: ``stale`` when unseen
    #: past the tenant's threshold); empty when the source does not say.
    status: Literal["", "active", "inactive", "agentless", "stale"] = ""
    #: An AI target (category ``ai_target``, #1501): OpenAEV's ``ai_target_*`` fields, lower-cased
    #: enums. The token, the configuration and the system prompt never leave OpenAEV.
    ai_target_provider: str = Field(default="", max_length=40)
    ai_target_endpoint: str = Field(default="", max_length=500)
    ai_target_model: str = Field(default="", max_length=200)
    ai_target_modality: Literal["", "text", "vision", "audio", "multimodal"] = ""

    @model_validator(mode="before")
    @classmethod
    def _legacy_type(cls, data: Any) -> Any:
        return upgrade_legacy_type(data) if isinstance(data, dict) else data

    @field_validator("ai_target_provider")
    @classmethod
    def _provider(cls, value: str) -> str:
        text = value.strip().lower()
        if text and text not in AI_TARGET_PROVIDERS:
            raise ValueError(f"ai_target_provider {value!r} is not one of {', '.join(AI_TARGET_PROVIDERS)}")
        return text

    @field_validator("mac", "ip")
    @classmethod
    def _clean_addresses(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(v.strip()[:64] for v in value if v and v.strip()))

    @field_validator("products", "tags")
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


class GroupMembershipRule(BaseModel):
    """Which assets join an asset group without naming it in their ``group`` (#1773).

    An asset matches when it carries one of ``tags``, is of one of
    ``categories``, or its name holds ``name_contains`` (case-insensitive);
    an empty rule matches nothing, so a group without one keeps its members
    by name only, as before.
    """

    model_config = _Strict

    tags: list[str] = Field(default_factory=list, max_length=50)
    categories: list[str] = Field(default_factory=list, max_length=20)
    name_contains: str = Field(default="", max_length=200)

    @field_validator("tags", "categories")
    @classmethod
    def _clean(cls, value: list[str]) -> list[str]:
        return _names(value)

    def empty(self) -> bool:
        return not (self.tags or self.categories or self.name_contains)

    def matches(self, asset: AssetEntry) -> bool:
        """Whether *asset* joins the group by this rule."""
        if self.empty():
            return False
        tags = {t.lower() for t in asset.tags}
        if tags & {t.lower() for t in self.tags}:
            return True
        if asset.category in self.categories:
            return True
        needle = self.name_contains.lower()
        return bool(needle) and needle in asset.name.lower()


class AssetGroupEntry(BaseModel):
    """A group of assets (OpenAEV's static asset group, #11781): assets join it through their ``group``.

    The fields an ``asset_group`` row of #11543 carried are kept, so the move
    from ``assets_exposure`` loses nothing; a nested ``group`` (OpenAEV has no
    nested groups) is kept as the tag ``group:<name>``. Since #1773 a group
    also carries the criticality its members take when they have none of
    their own, and a membership rule (:class:`GroupMembershipRule`).
    """

    model_config = _Strict

    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=1000)
    #: The criticality a member of unknown criticality takes (#1773; ``unknown``: none given).
    criticality: Literal["unknown", "low", "medium", "high", "very_high"] = "unknown"
    #: Assets that join the group by rule, besides those naming it in ``group`` (#1773).
    membership_rule: GroupMembershipRule = Field(default_factory=GroupMembershipRule)
    environment: Literal["", "production", "staging", "development", "test", "other"] = ""
    internet_facing: Literal["yes", "no", "unknown"] = "unknown"
    owner: str = Field(default="", max_length=200)
    products: list[str] = Field(default_factory=list, max_length=100)
    tags: list[str] = Field(default_factory=list, max_length=50)
    source: str = Field(default="manual", max_length=80)
    external_id: str = Field(default="", max_length=200)
    #: The sync that last wrote the row (#1134).
    sync_id: str = Field(default="", max_length=64)

    @field_validator("products", "tags")
    @classmethod
    def _clean_names(cls, value: list[str]) -> list[str]:
        return _names(value)


_CRITICALITY_RANK: tuple[str, ...] = ("unknown", "low", "medium", "high", "very_high")


def groups_of(profile: Any, asset: AssetEntry) -> list[AssetGroupEntry]:
    """The asset groups *asset* belongs to: the one its ``group`` names, and every group whose rule it matches (#1773)."""
    named = asset.group.strip().lower()
    return [
        group
        for group in getattr(profile, "asset_groups", None) or []
        if (named and group.name.lower() == named) or group.membership_rule.matches(asset)
    ]


def effective_criticality(profile: Any, asset: AssetEntry) -> str:
    """*asset*'s criticality, else the highest its groups give their members (#1773); ``unknown`` when none does."""
    if asset.criticality != "unknown":
        return asset.criticality
    given = [g.criticality for g in groups_of(profile, asset) if g.criticality != "unknown"]
    return max(given, key=_CRITICALITY_RANK.index) if given else "unknown"


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
    #: Keep only findings on assets linked to a primary asset of at least this criticality (empty: any).
    min_primary_asset_criticality: Literal["", "low", "medium", "high", "critical"] = ""
    exclusions: list[PolicyExclusion] = Field(default_factory=list, max_length=200)
    accepted_risks: list[AcceptedRisk] = Field(default_factory=list, max_length=500)
    #: CTEM scope gate (#847): a confirmed signal opens a case when its subject's criticality (assets,
    #: primary assets, risk scenarios) is at least this; below, it goes to the watchlist.
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
    #: CTEM review step (CTEM-16): the label an analyst sets on the drafted Grouping in OpenCTI to
    #: approve its coverage request when the gate ``ctem.analyst_review`` waits (empty: analyst-reviewed).
    analyst_review_label: str = Field(default="", max_length=100)
    #: CTEM-29: the scope criticality of an identity of an owned domain (a leaked credential, path 10).
    identity_criticality: Literal["low", "medium", "high", "critical"] = "high"


#: The values the settings page proposes for a first policy (never applied unless saved).
RECOMMENDED_POLICY: dict[str, Any] = {
    "cvss_min": 7.0,
    "epss_min": 0.1,
    "kev_always_in": True,
    "internet_exposure_required": False,
    "min_primary_asset_criticality": "",
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


class PirDetail(BaseModel):
    """What one PIR of ``pir_ids`` asks and who answers for it (#1773).

    OpenCTI keeps the PIR itself; the profile keeps what the organization
    says about it: the intelligence question, its owner, how often it is
    reviewed and the primary assets it serves (by name).
    """

    model_config = _Strict

    question: str = Field(default="", max_length=1000)
    owner: str = Field(default="", max_length=200)
    review_cadence: Literal["", "weekly", "monthly", "quarterly", "half_yearly", "yearly"] = ""
    primary_assets: list[str] = Field(default_factory=list, max_length=50)

    @field_validator("primary_assets")
    @classmethod
    def _clean(cls, value: list[str]) -> list[str]:
        return _names(value)


class FxRate(BaseModel):
    """A reference exchange rate an agent may cite to convert an amount (#1383).

    One unit of ``from_currency`` is ``rate`` units of ``to_currency``, as
    published on ``as_of`` by ``source``. A risk figure priced in another
    currency than the organization's (OpenCRQ prices in USD) is converted with
    the rate whose ``to_currency`` is the profile's currency before it is
    compared with a threshold; without one the comparison is unknown, never
    made across two currencies and never on a rate the model guessed.
    """

    model_config = _Strict

    from_currency: str = Field(min_length=3, max_length=3)
    to_currency: str = Field(min_length=3, max_length=3)
    rate: float = Field(gt=0)
    as_of: date
    source: str = Field(min_length=1, max_length=200)

    @field_validator("from_currency", "to_currency", mode="before")
    @classmethod
    def _iso(cls, value: Any) -> str:
        code = str(value or "").strip().upper()
        if not _CURRENCY.match(code):
            raise ValueError(f"currency {value!r} is not an ISO 4217 code (e.g. EUR, USD)")
        return code

    @model_validator(mode="after")
    def _two_currencies(self) -> FxRate:
        if self.from_currency == self.to_currency:
            raise ValueError(f"a reference rate converts between two currencies, not {self.from_currency} to itself")
        return self


def reference_rate(profile: OrganizationProfile, from_currency: str) -> FxRate | None:
    """The reference rate converting *from_currency* into the profile's currency, the latest ``as_of`` first.

    ``None`` when the profile has no currency, names the same one, or holds no
    such rate: the caller then reports the comparison as unknown.
    """
    source = str(from_currency or "").strip().upper()
    target = profile.currency
    if not target or not source or source == target:
        return None
    rates = [r for r in profile.fx_rates if r.from_currency == source and r.to_currency == target]
    return max(rates, key=lambda r: r.as_of) if rates else None


class TicketingSync(BaseModel):
    """How CTEM cases sync with the ticketing target (#917, ``dev-docs/ctem-ticketing.md``).

    Read by ``services/ctem_ticketing.py`` at every sync, so a change applies
    to the next sync with no restart. Every field has the program default.
    """

    model_config = _Strict

    #: When a case gets its ticket: when it passes Mobilize and is P1 or P2, P1 only, or by hand.
    create_on: Literal["mobilize_p1_p2", "mobilize_p1", "manual"] = "mobilize_p1_p2"
    #: The Jira issue type, or the ServiceNow table (``incident``, ``change_request`` or ``problem``,
    #: ``ctem_ticketing.SERVICENOW_TABLES``; another table is refused at save).
    record_type: str = Field(default="", max_length=80)
    #: Labels (Jira) or the category (ServiceNow) every ticket carries.
    labels: list[str] = Field(default_factory=list, max_length=20)
    #: Case severity to the system's priority value (Jira priority name, ServiceNow urgency 1-3).
    priority_map: dict[str, str] = Field(default_factory=dict)
    #: The ticket's own status name to a case ticket status (open, in_progress, done, cancelled),
    #: read before the system's default categories.
    status_map: dict[str, str] = Field(default_factory=dict)
    #: A ticket moved to done asks the case's re-test.
    done_starts_retest: bool = True
    #: A case reopened by a failed re-test reopens its ticket, or opens a linked one.
    on_reopen: Literal["reopen", "linked"] = "reopen"
    #: Put the case's due date (its SLA) on the ticket.
    due_date_from_sla: bool = True


class AttackSurfacePass(BaseModel):
    """The opt-in outside lens of CTEM-26 (``dev-docs/ctem-attack-surface.md``).

    Off by default. When on, a scheduled pass reads the outside view of
    ``owned_domains`` and ``address_ranges`` from ``integration`` and
    ``integrations`` (connected attack-surface providers: Shodan, Google Threat
    Intelligence ASM), merges them per host and records the differences as
    signals on paths 5, 6 and 7.
    """

    model_config = _Strict

    enabled: bool = False
    #: The id of a provider integration the pass reads with its owner's credential (the first one).
    integration: str = Field(default="", max_length=100)
    #: More provider integrations read in the same pass, their observations merged per host.
    integrations: list[Annotated[str, Field(max_length=100)]] = Field(default_factory=list, max_length=10)
    #: Hours between two passes (daily by default).
    cadence_hours: int = Field(default=24, ge=1, le=24 * 30)
    #: A host unseen for this many days is a stale asset (path 6).
    stale_after_days: int = Field(default=14, ge=1, le=365)


class EvidenceHealthPass(BaseModel):
    """The evidence health pass of CTEM-28 (``dev-docs/ctem-evidence-health.md``).

    A scheduled pass reads from ``integration`` (an OpenCRQ integration) the
    evidence health of the supporting assets of every risk scenario and records
    evidence decay (path 9) and blind spots (path 12) as signals. A gap never
    reads as safe.

    ``enabled`` unset (``None``) means no administrator chose: the pass runs on
    the declared integration, else on the single connected OpenCRQ integration
    (#1344, ``ctem_evidence_health.resolve_pass``). ``False`` keeps it off,
    except the legacy stored default (``False`` with no integration), read as unset.
    """

    model_config = _Strict

    enabled: bool | None = None
    #: The id of the OpenCRQ integration the pass reads with its owner's credential.
    integration: str = Field(default="", max_length=100)
    #: The re-test delay: evidence older than this many days has expired (path 9).
    max_age_days: int = Field(default=30, ge=1, le=3650)
    #: Hours between two passes (daily by default).
    cadence_hours: int = Field(default=24, ge=1, le=24 * 30)


#: One source of a trigger path as ``provider:kind`` (``ctem_signal_sources.source_key``).
_SOURCE_KEY = re.compile(r"^[a-z0-9_-]{1,40}:(listener|pass|pir_playbook)(:[a-z0-9_]{1,40})?$")


class SignalSourcesSettings(BaseModel):
    """Which connected source feeds which CTEM trigger path (#1445, ``dev-docs/ctem-signals.md`` s.2bis).

    Every connected source feeds every path it can by default. ``excluded``
    names, per trigger path, the sources (``provider:kind``) an administrator
    took off that path alone: an excluded source records nothing on it and is
    not bound on it, while it keeps feeding its other paths. ``included`` names
    the sources off by default (#2436) an administrator switched on for a path.
    ``pirs`` names, per path a PIR playbook feeds, the OpenCTI PIRs it listens
    to (#2436): a path absent takes its default (``ctem_signal_sources.pir_selection``),
    an empty list provisions no playbook for that path.
    Set from the Sources tab of CTEM > Signals.
    """

    model_config = _Strict

    excluded: dict[str, list[str]] = Field(default_factory=dict)
    included: dict[str, list[str]] = Field(default_factory=dict)
    pirs: dict[str, list[str]] = Field(default_factory=dict)

    @field_validator("pirs")
    @classmethod
    def _clean_pirs(cls, value: dict[str, list[str]]) -> dict[str, list[str]]:
        from app.services.ctem_signal_sources import PIR_PATHS

        out: dict[str, list[str]] = {}
        for path, ids in value.items():
            if path not in PIR_PATHS:
                raise ValueError(f"{path!r} is not a trigger path a PIR playbook feeds")
            cleaned: list[str] = []
            for pid in ids:
                pid = str(pid).strip()
                if not pid or len(pid) > 100:
                    raise ValueError(f"{pid!r} is not a PIR id")
                if pid not in cleaned:
                    cleaned.append(pid)
            # An empty list is kept: it switches the path's playbook off.
            out[path] = sorted(cleaned)
        return out

    @field_validator("excluded", "included")
    @classmethod
    def _clean_excluded(cls, value: dict[str, list[str]]) -> dict[str, list[str]]:
        from app.services.ctem_signal_sources import PATH_SOURCES

        out: dict[str, list[str]] = {}
        for path, keys in value.items():
            known = {s.key for s in PATH_SOURCES.get(path, ())}
            if not known:
                raise ValueError(f"{path!r} is not a trigger path with a source")
            cleaned: list[str] = []
            for key in keys:
                key = str(key).strip()
                if not _SOURCE_KEY.match(key) or key not in known:
                    raise ValueError(f"{key!r} is not a source of the trigger path {path!r}")
                if key not in cleaned:
                    cleaned.append(key)
            if cleaned:
                out[path] = sorted(cleaned)
        return out


class CasesRecompute(BaseModel):
    """How often the shared CTEM cases are recomputed from the source products (``dev-docs/ctem-cases.md`` s.4.3).

    On by default: cases are inferred, never refreshed by hand. The scheduled
    pass reads with the CTEM loop fleet's run-as users, else the recent viewers
    of Cases; a viewer opening the page past the interval gets one too.
    """

    model_config = _Strict

    #: Minutes between two scheduled recomputes (every 15 minutes by default, at least 5).
    interval_minutes: int = Field(default=15, ge=5, le=24 * 60)


class CaseAttentionWeights(BaseModel):
    """How the CTEM Cases list ranks what needs attention first (#1176, ``dev-docs/ctem-cases.md`` s.7.2bis).

    Points per reason of the explained attention score; the defaults are the
    ranking the list ships with. Set by an administrator from the "Act on this
    first" band of CTEM > Cases. Read by the Cases list and the case page only.
    """

    model_config = _Strict

    priority_critical: int = Field(default=40, ge=0, le=100)
    priority_high: int = Field(default=25, ge=0, le=100)
    priority_medium: int = Field(default=10, ge=0, le=100)
    priority_low: int = Field(default=0, ge=0, le=100)
    #: Points when the case holds at least one KEV issue.
    kev: int = Field(default=15, ge=0, le=100)
    #: Points when the case touches at least one primary asset.
    primary_asset: int = Field(default=15, ge=0, le=100)
    #: Points when the SLA is breached or the due date has passed.
    overdue: int = Field(default=20, ge=0, le=100)
    #: Points when the due date is within ``due_soon_days``.
    due_soon: int = Field(default=10, ge=0, le=100)
    due_soon_days: int = Field(default=3, ge=1, le=90)
    #: Points when nobody owns the case.
    no_owner: int = Field(default=5, ge=0, le=100)
    #: Points per day open, capped at ``age_max``.
    age_per_day: int = Field(default=1, ge=0, le=100)
    age_max: int = Field(default=10, ge=0, le=100)


class AssetIdentity(BaseModel):
    """How the asset store matches one asset across sources and which source a field shows (#1178).

    Empty keeps the defaults of ``organization_asset_identity``: every strong key in its default
    order, and ``DEFAULT_PRECEDENCE`` per field. Set in CTEM > Inventory (Sources panel).
    """

    model_config = _Strict

    #: The strong keys, most reliable first; a key left out never merges (empty: the default order).
    key_order: list[Literal["serial", "cloud_resource_id", "repository", "device_id", "mac", "fqdn", "hostname"]] = (
        Field(default_factory=list, max_length=7)
    )
    #: Per field, the sources or source classes in order (``*`` any other); a field left out keeps its default.
    precedence: dict[
        Annotated[str, Field(min_length=1, max_length=80)],
        list[Annotated[str, Field(min_length=1, max_length=80)]],
    ] = Field(default_factory=dict, max_length=50)

    @field_validator("key_order")
    @classmethod
    def _distinct_keys(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(value))

    @field_validator("precedence")
    @classmethod
    def _clean_precedence(cls, value: dict[str, list[str]]) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for name, order in value.items():
            cleaned = list(dict.fromkeys(s.strip().lower() for s in order if s.strip()))
            if len(cleaned) > 20:
                raise ValueError(f"precedence of {name!r} names more than 20 sources")
            if cleaned:
                out[name.strip().lower()] = cleaned
        return out


def normalize_address_ranges(value: list[str]) -> list[str]:
    """CIDR blocks in their network form, each once (a bare address is its /32 or /128)."""
    import ipaddress

    out: list[str] = []
    for raw in value:
        text = str(raw or "").strip()
        if not text:
            continue
        try:
            network = ipaddress.ip_network(text, strict=False)
        except ValueError as exc:
            raise ValueError(f"address range {text!r} is not an IP address or CIDR block") from exc
        out.append(str(network))
    return list(dict.fromkeys(out))


class TicketingTarget(BaseModel):
    model_config = _Strict

    integration: str = Field(default="", max_length=100)
    project: str = Field(default="", max_length=200)
    sync: TicketingSync = Field(default_factory=TicketingSync)


class OrganizationProfile(BaseModel):
    """The organization profile as stored and served (every field optional)."""

    model_config = _Strict

    name: str = Field(default="", max_length=200)
    sector: str = Field(default="", max_length=100)
    sub_sector: str = Field(default="", max_length=100)
    regions: list[str] = Field(default_factory=list, max_length=250)
    size: Literal["", "1-50", "51-250", "251-1000", "1001-5000", "5001-20000", "20000+"] = ""
    primary_assets: list[PrimaryAsset] = Field(default_factory=list, max_length=200)
    software: list[SoftwareEntry] = Field(default_factory=list, max_length=500)
    #: What the organization ships, with its source repositories (#1432). Not a contract
    #: field: a vendor-free organization ships nothing; the software is what it runs.
    shipped_products: list[ShippedProduct] = Field(default_factory=list, max_length=200)
    pir_ids: list[str] = Field(default_factory=list, max_length=100)
    #: The last known OpenCTI name of each PIR in ``pir_ids``, saved with it so the
    #: profile names a PIR even when OpenCTI cannot be read or deleted it (#9615).
    #: Not a contract field: ``missing_fields`` ignores it.
    pir_names: dict[str, str] = Field(default_factory=dict)
    #: What each PIR asks, its owner, cadence and the primary assets it serves, by PIR id (#1773).
    #: Not a contract field; shown to agents only when filled.
    pir_details: dict[str, PirDetail] = Field(default_factory=dict, max_length=100)
    #: The minimum PIR score of each PIR of ``pir_ids``, by PIR id (#2435): answered from the
    #: Intelligence policy (``services/organization_pirs.py``); a PIR without one uses the policy's
    #: global minimum of its kind. Not a contract field.
    pir_min_scores: dict[str, int] = Field(default_factory=dict, max_length=500)
    risk_appetite: RiskAppetite = Field(default_factory=RiskAppetite)
    loaded_hourly_rate: float | None = Field(default=None, ge=0)
    currency: str = Field(default="", max_length=3)
    #: Reference exchange rates into the organization's currency (#1383), read by the
    #: risk agents to convert OpenCRQ figures. Not a contract field; shown to agents
    #: only when filled.
    fx_rates: list[FxRate] = Field(default_factory=list, max_length=50)
    ticketing: TicketingTarget = Field(default_factory=TicketingTarget)
    #: Assets of the attack surface, with their exposure (#11543).
    assets_exposure: list[AssetEntry] = Field(default_factory=list, max_length=PROFILE_ASSET_CAP)
    #: Groups the assets join through their ``group`` (#11781; ``asset_group`` rows of #11543 move here).
    asset_groups: list[AssetGroupEntry] = Field(default_factory=list, max_length=500)
    #: The rules the prioritization funnel applies (#11543); defaults change nothing.
    prioritization_policy: PrioritizationPolicy = Field(default_factory=PrioritizationPolicy)
    #: Knowledge bases of the organization's procedures, read by tool only (#11543).
    enterprise_knowledge: list[KnowledgeLink] = Field(default_factory=list, max_length=50)
    #: The DNS and email domains the organization owns (CTEM-29; the outside lens reads them too):
    #: a leaked credential of an account on one of them is in scope.
    owned_domains: list[str] = Field(default_factory=list, max_length=500)
    #: The public address ranges the organization owns, as CIDR blocks (CTEM-26): an answering
    #: name that points outside them is no longer owned (path 6).
    address_ranges: list[str] = Field(default_factory=list, max_length=500)
    #: The opt-in attack surface pass of the outside lens (CTEM-26).
    attack_surface: AttackSurfacePass = Field(default_factory=AttackSurfacePass)
    #: The opt-in evidence health pass: evidence decay and blind spots (CTEM-28).
    evidence_health: EvidenceHealthPass = Field(default_factory=EvidenceHealthPass)
    #: How often the shared CTEM cases are recomputed with no user action.
    cases_recompute: CasesRecompute = Field(default_factory=CasesRecompute)
    #: The sources an administrator took off one trigger path (#1445).
    signal_sources: SignalSourcesSettings = Field(default_factory=SignalSourcesSettings)
    #: How one asset is matched across sources and which source each field shows (#1178).
    asset_identity: AssetIdentity = Field(default_factory=AssetIdentity)
    #: The points of the Cases attention score (#1176): what needs attention first.
    case_attention: CaseAttentionWeights = Field(default_factory=CaseAttentionWeights)
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

    @field_validator("owned_domains")
    @classmethod
    def _domains(cls, value: list[str]) -> list[str]:
        """Lower-cased, ``@`` and a trailing dot dropped, each once."""
        cleaned = (str(v).strip().lower().lstrip("@").rstrip(".") for v in value)
        return [d for d in dict.fromkeys(cleaned) if d and len(d) <= 253]

    @field_validator("address_ranges")
    @classmethod
    def _ranges(cls, value: list[str]) -> list[str]:
        return normalize_address_ranges(value)

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


def ai_target_assets(profile: OrganizationProfile) -> list[AssetEntry]:
    """The assets of category ``ai_target`` of *profile* (the AI targets OpenAEV tests)."""
    return [asset for asset in profile.assets_exposure if asset.category == "ai_target"]


#: Every field of the contract, in the order a report names them, with the
#: words a person reads when it is missing.
CONTRACT_FIELDS: tuple[tuple[str, str], ...] = (
    ("name", "organization name"),
    ("sector", "sector"),
    ("regions", "regions of operation"),
    ("size", "organization size"),
    ("primary_assets", "primary assets and business services"),
    ("software", "software"),
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


#: The identity fields: in Copilot mode the profile is these alone (#1690). Copilot has
#: no CTEM loop, so its agents need who the company is and nothing else; the other
#: fields stay stored untouched, so switching the mode never loses data.
IDENTITY_FIELDS: frozenset[str] = frozenset({"name", "sector", "sub_sector", "regions", "size"})


def identity_only() -> bool:
    """Whether the profile is read and reported as its identity alone: a Copilot-like platform (#1690)."""
    from app.core.config import settings

    return bool(settings.is_copilot_like)


def contract_fields() -> tuple[tuple[str, str], ...]:
    """The contract fields of this platform's mode: :data:`CONTRACT_FIELDS`, the identity alone in Copilot mode."""
    if identity_only():
        return tuple(field for field in CONTRACT_FIELDS if field[0] in IDENTITY_FIELDS)
    return CONTRACT_FIELDS


def missing_fields(profile: OrganizationProfile) -> list[str]:
    """The contract fields *profile* leaves empty, by path, in contract order (the identity alone in Copilot mode)."""
    missing: list[str] = []
    for path, _label in contract_fields():
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


#: The contract fields only the CTEM loop reads (scope gate, risk quantification,
#: prioritization, ticketing): a run that is not CTEM work is never pushed to
#: report them, and its briefs carry no notice about them (#1384).
CTEM_ONLY_FIELDS: frozenset[str] = frozenset(
    {
        "pir_ids",
        "risk_appetite.annual_loss_threshold",
        "risk_appetite.approval_threshold",
        "loaded_hourly_rate",
        "ticketing",
    }
)


def missing_for_run(missing: list[str], *, ctem_work: bool) -> list[str]:
    """*missing* (contract paths) as a run reads it: the CTEM-only fields left out when it is not CTEM work."""
    return list(missing) if ctem_work else [path for path in missing if path not in CTEM_ONLY_FIELDS]


def holds_ctem_tools(agent: object) -> bool:
    """Whether *agent* holds CTEM tools: a built-in CTEM case or signal agent, or the ``ctem`` group bound (a former ``ctem_read`` id included, #2443)."""
    from app.services.builtin_tool_aliases import current_group_id
    from app.services.builtin_tools.ctem import CTEM_GROUP
    from app.services.builtin_tools.ctem_cases import CTEM_CASE_AGENT_SLUGS
    from app.services.builtin_tools.ctem_signals import CTEM_SIGNAL_AGENT_SLUGS

    if agent is None:
        return False
    groups = getattr(agent, "builtin_tool_ids", None)
    if isinstance(groups, (list, tuple, set, frozenset)) and CTEM_GROUP in {current_group_id(str(g)) for g in groups}:
        return True
    slug = getattr(agent, "slug", None) or ""
    return bool(getattr(agent, "is_builtin", False)) and slug in (CTEM_CASE_AGENT_SLUGS | CTEM_SIGNAL_AGENT_SLUGS)


def is_ctem_fleet(*, slug: object, is_builtin: object, forked_from: object) -> bool:
    """Whether a fleet is a CTEM fleet: a seeded CTEM fleet (a retired slug included) or a fork of one."""
    from app.agents.builtin_fleets import BUILTIN_FLEETS, RETIRED_FLEET_SUCCESSORS

    slugs = {entry["slug"] for entry in BUILTIN_FLEETS} | set(RETIRED_FLEET_SUCCESSORS)
    if is_builtin and slug in slugs:
        return True
    source = forked_from if isinstance(forked_from, dict) else {}
    return bool(source.get("is_builtin")) and source.get("slug") in slugs


async def ctem_fleet(db: AsyncSession, fleet_id: object) -> bool:
    """Whether the fleet *fleet_id* is a CTEM fleet; ``False`` for none or a failed read."""
    from app.models.fleet import Fleet

    if fleet_id is None:
        return False
    try:
        row = (
            await db.execute(select(Fleet.slug, Fleet.is_builtin, Fleet.forked_from).where(Fleet.id == fleet_id))
        ).first()
    except Exception:  # context, never a reason to fail a run: read as not CTEM
        return False
    if row is None:
        return False
    return is_ctem_fleet(slug=row[0], is_builtin=row[1], forked_from=row[2])


async def ctem_work_for(db: AsyncSession, agent: object, *, fleet_id: object = None) -> bool:
    """Whether a run of *agent* is CTEM work (#1384): it holds CTEM tools, or its home fleet is a CTEM fleet.

    *fleet_id* is the home fleet the caller already resolved; without it the
    home fleet is read. The one signal every organization block reads before
    it pushes the CTEM-only fields and the brief notice.
    """
    if holds_ctem_tools(agent):
        return True
    if agent is None:
        return False
    if fleet_id is None:
        from app.services.fleet_runs import home_fleet_defaults

        fleet_id = getattr(await home_fleet_defaults(db, getattr(agent, "id", None)), "fleet_id", None)
    return await ctem_fleet(db, fleet_id)


def brief_notice_instruction(missing: list[str]) -> str:
    """What the agent is told to add to its briefs when *missing* (contract paths) names a field.

    ``""`` for a complete profile: a brief then carries no notice. Implemented
    once here and read by every profile block an agent gets (the basic
    summary of every turn and the ``get_organization_context`` reading),
    only on a run that is CTEM work (:func:`ctem_work_for`, #1384).
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


#: The prioritization policy rules an agent may set or propose, one path each (#937, #2129): the scope
#: threshold and the scalar rules (thresholds, the KEV override, the time to fix per severity). The lists
#: (exclusions, accepted risks) stay with the settings form.
POLICY_SETTABLE_PATHS: tuple[str, ...] = (
    "prioritization_policy.scope_min_criticality",
    "prioritization_policy.cvss_min",
    "prioritization_policy.epss_min",
    "prioritization_policy.kev_always_in",
    "prioritization_policy.internet_exposure_required",
    "prioritization_policy.min_primary_asset_criticality",
    "prioritization_policy.require_risk_scenario",
    "prioritization_policy.sla_hours",
    "prioritization_policy.pir_min_score_vulnerability",
    "prioritization_policy.pir_min_score_threat",
    "prioritization_policy.identity_criticality",
)

#: The settable paths beyond the contract fields whose provenance a save keeps too (#1773): what an
#: agent or the API may write (``organization_context_proposals``), so every written field says who and why.
PROVENANCE_EXTRA_PATHS: tuple[str, ...] = (
    *POLICY_SETTABLE_PATHS,
    "owned_domains",
    "address_ranges",
    "attack_surface",
    "fx_rates",
    "shipped_products",
    "enterprise_knowledge",
    "pir_details",
)


def accepted_sources(raw: dict[str, Any] | None) -> dict[str, dict[str, str]] | None:
    """The provenance an ``ai_draft`` save keeps: the settable fields only, ``source`` and ``basis`` bounded.

    An entry may also carry ``confirmed_at`` (#1773): the value was confirmed
    unchanged at that time, by ``source``, on ``basis``.
    """
    paths = {path for path, _label in CONTRACT_FIELDS} | set(PROVENANCE_EXTRA_PATHS)
    kept: dict[str, dict[str, str]] = {}
    for path, entry in (raw or {}).items():
        if path not in paths or not isinstance(entry, dict):
            continue
        kept[path] = {
            "source": str(entry.get("source") or "")[:300],
            "basis": str(entry.get("basis") or "")[:500],
        }
        if entry.get("confirmed_at"):
            kept[path]["confirmed_at"] = str(entry["confirmed_at"])[:40]
    return kept or None


def rate_notice(profile: OrganizationProfile) -> str:
    """A line naming the missing reference rates (#1773; ``""`` when none is missing).

    A profile with a currency and no reference rate into it cannot convert an
    OpenCRQ figure: the agent is told so rather than left to guess a rate.
    """
    if not profile.currency or any(r.to_currency == profile.currency for r in profile.fx_rates):
        return ""
    return f"No reference exchange rate into {profile.currency} (fx_rates)."


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


async def asset_list_counts(db: AsyncSession, organization_id: uuid.UUID | None) -> dict[str, int] | None:
    """``{assets_exposure, asset_groups}`` counted on the asset records (#1543), ``None`` before their import.

    Two indexed ``COUNT`` reads: the prompt blocks and the category index name
    the true sizes without loading a row (the profile JSON no longer sees the
    row API, agent and import writes).
    """
    from app.services import organization_asset_records as records

    if organization_id is None or not await records.is_imported(db, organization_id):
        return None
    return await records.list_counts(db, organization_id)


async def agent_list_counts(db: AsyncSession, organization_id: uuid.UUID | None) -> dict[str, int] | None:
    """The category sizes an agent reads, counted from what ``get_organization_context`` pages (#2144).

    :func:`asset_list_counts`, then ``assets_exposure`` from the merged
    inventory (one asset per identity cluster) and ``software`` from the
    Software entities, each once it holds a row: the paged read answers from
    those same stores, so the summary, the ``*`` index and the "of N" of a
    page name one size. Read only, never builds a store.
    """
    from app.models.organization_asset import OrganizationAsset, OrganizationSoftware

    if organization_id is None:
        return None
    counts = dict(await asset_list_counts(db, organization_id) or {})
    for name, model in (("assets_exposure", OrganizationAsset), ("software", OrganizationSoftware)):
        held = (
            await db.execute(select(func.count()).select_from(model).where(model.organization_id == organization_id))
        ).scalar() or 0
        if held:
            counts[name] = int(held)
    return counts or None


async def hydrated_profile(db: AsyncSession, row: OrganizationContextVersion | None) -> OrganizationProfile:
    """The profile of *row* with ``assets_exposure`` and ``asset_groups`` read from the asset records (#1543).

    The records are the source of truth once imported: a row API, agent or
    import write lands there and never in a profile version. Before the import
    the stored lists are the truth and are answered as they are. The lists are
    capped at 5,000 assets (``profile_lists``), the bound the readers had.
    """
    from app.services import organization_asset_records as records
    from app.services.organization_pirs import overlay_profile

    profile = parse_profile(row.profile if row else {})
    if row is None or row.organization_id is None or not await records.is_imported(db, row.organization_id):
        # The PIR fields come from the Intelligence policy once PIRs are stored (#2435).
        return await overlay_profile(db, profile) if row is not None else profile
    asset_rows, group_rows, _truncated = await records.profile_lists(db, row.organization_id, cap=PROFILE_ASSET_CAP)
    assets: list[AssetEntry] = []
    for value in asset_rows:
        try:
            assets.append(AssetEntry.model_validate({k: v for k, v in value.items() if k != "software"}))
        except ValueError:
            continue  # a record a newer release wrote with a field this one does not know
    groups: list[AssetGroupEntry] = []
    for value in group_rows:
        try:
            groups.append(AssetGroupEntry.model_validate(value))
        except ValueError:
            continue
    return await overlay_profile(db, profile.model_copy(update={"assets_exposure": assets, "asset_groups": groups}))


async def current_profile(
    db: AsyncSession, organization_id: uuid.UUID | None = None
) -> tuple[OrganizationContextVersion | None, OrganizationProfile]:
    """``(latest version, its profile with the asset lists from the records)``: what a reader of assets uses."""
    row = await current_version(db, organization_id)
    return row, await hydrated_profile(db, row)


async def software_profile(
    db: AsyncSession, organization_id: uuid.UUID | None = None
) -> tuple[OrganizationContextVersion | None, OrganizationProfile]:
    """:func:`current_profile` with its software read from the Software entities (#1495), for the CTEM gates.

    The software the organization runs is the Software entities of the asset
    store (synced from Intune, Defender, OpenAEV and the SBOMs, or typed by an
    administrator), plus the profile rows typed before the move that no entity
    carries (``organization_software_store.matcher_entries``, the CVE matcher's
    reading). Read-only: the answer is never saved, a writer reads
    :func:`current_version`. Before the store holds any software the profile
    rows are answered as they are.
    """
    from app.services import organization_software_store as software_store

    row, profile = await current_profile(db, organization_id)
    if row is None or row.organization_id is None:
        return row, profile
    software = await software_store.matcher_software(db, row.organization_id)
    if not software:
        return row, profile
    typed = [{"vendor": e.vendor, "product": e.product, "cpe": e.cpe, "version": e.version} for e in profile.software]
    # Pure CPU over every software of the store (thousands on a large estate): built in a worker
    # thread so the web event loop keeps serving requests meanwhile.
    entries = await asyncio.to_thread(_software_entries, software, typed)
    # model_copy does not validate: the store may hold more software than a typed list may.
    return row, profile.model_copy(update={"software": entries})


def _software_entries(software: list[dict[str, Any]], typed: list[dict[str, Any]]) -> list[SoftwareEntry]:
    """The profile's software rows for the store's *software* and the *typed* rows (pure)."""
    from app.services import organization_software_store as software_store

    return [
        SoftwareEntry(
            product=entry["product"][:200],
            vendor=entry["vendor"][:200],
            cpe=entry["cpe"][:300],
            version=entry["version"][:100],
            source="software",
        )
        for entry in software_store.matcher_entries(software, typed)
    ]


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


async def get_version(
    db: AsyncSession, version: int, organization_id: uuid.UUID | None = None
) -> OrganizationContextVersion | None:
    """Version *version* of *organization_id* (the primary when ``None``), or ``None`` (#1473)."""
    return (
        await db.execute(
            select(OrganizationContextVersion)
            .where(_of_organization(organization_id), OrganizationContextVersion.version == version)
            .limit(1)
        )
    ).scalar_one_or_none()


def restored_profile(
    current: dict[str, Any], past: dict[str, Any], fields: list[str]
) -> tuple[dict[str, Any], list[str]]:
    """*current* with *fields* taken from *past* (every field of *past* when empty); ``(profile, fields)``.

    ``ValueError`` names a field the profile does not have.
    """
    known = set(OrganizationProfile.model_fields)
    wanted = [f for f in dict.fromkeys(str(f).strip() for f in fields) if f] or sorted(known)
    unknown = [f for f in wanted if f not in known]
    if unknown:
        raise ValueError(f"not a field of the organization profile: {', '.join(unknown)}")
    restored = dict(current)
    for name in wanted:
        if name in past:
            restored[name] = past[name]
        else:
            restored.pop(name, None)
    return restored, wanted


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
    # Since #1543 the asset records are the source of truth: the asset and group lists of the saved
    # profile (an old client, an import or a sync still on this path) become row writes first, only
    # the rows that differ, each re-matching its own identity cluster. A hand row gets its id written
    # back on *profile* before the dump, so the next save addresses it exactly.
    from app.services import organization_asset_records as records

    if await _asset_lists_changed(db, organization_id, profile, latest):
        held = await asset_list_counts(db, organization_id)
        await records.apply_profile_lists(
            db,
            organization_id,
            profile,
            origin="profile",
            actor=f"{source}:{author_user_id or ''}".rstrip(":"),
            profile_version=int(latest or 0) + 1,
            # A list read from records past the model's cap is a page: what it lacks is not deleted.
            remove_missing=not held or held["assets_exposure"] <= PROFILE_ASSET_CAP,
        )
    elif await records.is_imported(db, organization_id):
        if await records.imported_version(db, organization_id) >= int(latest or 0):
            await records.mark_version(db, organization_id, int(latest or 0) + 1)
    # #2583 point 7: the signal recorders cache the source exclusions; a new profile version drops them.
    from app.services.ctem_signal_sources import forget_exclusions

    forget_exclusions()
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


def _list_dump(rows: Any) -> list[Any]:
    return [r.model_dump(mode="json") if hasattr(r, "model_dump") else dict(r) for r in rows or []]


async def _asset_lists_changed(
    db: AsyncSession, organization_id: uuid.UUID, profile: BaseModel, latest: int | None
) -> bool:
    """Whether *profile* changes the asset or group list of the latest stored version (#1543).

    A writer that edits another field bases its profile on the stored JSON,
    whose lists no longer see the row API, agent and import writes. Applying
    those unchanged, stale lists to the records would delete what those writes
    added: only a list the caller changed is applied. A profile without the
    lists (a tracked organization's model) changes nothing.
    """
    if getattr(profile, "assets_exposure", None) is None:
        return False
    if not latest:
        return True
    stored = (
        await db.execute(
            select(OrganizationContextVersion.profile).where(
                OrganizationContextVersion.organization_id == organization_id,
                OrganizationContextVersion.version == int(latest),
            )
        )
    ).scalar()
    stored = stored if isinstance(stored, dict) else {}
    for name in ("assets_exposure", "asset_groups"):
        before = [dict(r) for r in stored.get(name) or [] if isinstance(r, dict)]
        if _list_dump(getattr(profile, name, None)) != [
            _list_dump([parsed])[0] for parsed in _parsed_rows(name, before)
        ]:
            return True
    return False


def _parsed_rows(name: str, rows: list[dict[str, Any]]) -> list[Any]:
    """*rows* of the stored list *name* as the profile model dumps them (a row that no longer parses kept raw).

    A source's inline ``software`` (not an ``AssetEntry`` field) is left out, as
    the model drops it, so an unchanged synced row compares equal.
    """
    from app.services import organization_asset_records as records

    out: list[Any] = []
    for row in rows:
        try:
            if name == "assets_exposure":
                value = records.validate_asset(row)
                value.pop("software", None)
                out.append(AssetEntry.model_validate(value))
            else:
                out.append(AssetGroupEntry.model_validate(row))
        except ValueError:
            out.append(row)
    return out


#: Header of the block a fleet run's prompt carries (see :func:`fleet_run_context`).
FLEET_RUN_CONTEXT_HEADER = "--- Organization context ---"


async def fleet_run_context(
    db: AsyncSession,
    agent_id: object,
    *,
    fleet: object | None = None,
    organization_ids: list[uuid.UUID] | None = None,
    agent: object = None,
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
    follow the primary's, so the run names the organization. *agent* (the run's
    agent row) and the home fleet decide whether the run is CTEM work, the
    only runs told about the CTEM-only fields and the brief notice (#1384).
    """
    import logging

    from app.core.utils import format_exception_message
    from app.services.fleet_runs import home_fleet_defaults

    try:
        resolved = fleet if fleet is not None else await home_fleet_defaults(db, agent_id)
        if getattr(resolved, "fleet_id", None) is None:
            return ""
        ctem_work = holds_ctem_tools(agent) or await ctem_fleet(db, getattr(resolved, "fleet_id", None))
        primary = await current_version(db)
        block = render_for_agent(
            primary,
            ctem_work=ctem_work,
            asset_counts=await agent_list_counts(db, primary.organization_id if primary else None),
        )
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


def _row_identity(row: dict[str, Any]) -> tuple[str, str]:
    """How a list row of the profile is told apart: its source key when synced, else its name."""
    external_id = str(row.get("external_id") or "").strip()
    if external_id:
        return str(row.get("source") or "").strip().lower(), external_id
    name = row.get("name") or row.get("product") or row.get("id") or ""
    return "", str(name).strip().casefold()


def _row_label(row: dict[str, Any]) -> str:
    return str(row.get("name") or row.get("product") or row.get("external_id") or row.get("id") or "?")[:200]


def removed_rows(before: dict[str, Any] | None, after: dict[str, Any]) -> dict[str, list[str]]:
    """List field -> the names of the rows *before* held that *after* no longer holds (#1473).

    Both are dumped profiles. Rows are matched on ``source`` + ``external_id``
    when synced, else on their name, so a save that drops a primary asset (a
    stale form, an API client sending an old copy) says which ones, never
    silently. A list field absent from *after* is not judged.
    """
    if not before:
        return {}
    out: dict[str, list[str]] = {}
    for key, rows in before.items():
        if not isinstance(rows, list) or key not in after or not isinstance(after.get(key), list):
            continue
        old = [r for r in rows if isinstance(r, dict)]
        if not old:
            continue
        kept = {_row_identity(r) for r in after[key] if isinstance(r, dict)}
        lost = [_row_label(r) for r in old if _row_identity(r) not in kept]
        if lost:
            out[key] = lost[:100]
    return out


def _drop_typed_provenance(row: dict[str, Any]) -> None:
    """Leave out the provenance fields of *row* that hold their typed-row defaults (#1134)."""
    if row.get("source") == "manual":
        row.pop("source", None)
    for key in ("external_id", "sync_id"):
        if not row.get(key):
            row.pop(key, None)


def legacy_dump(profile: OrganizationProfile) -> dict[str, Any]:
    """The profile as dumped before #11543: the parts it added left out, an empty primary asset link too.

    An empty software version (CTEM-11) is left out the same way, and so are
    empty reference exchange rates (#1383), and
    ticketing sync settings left at their defaults (#917) and the provenance of
    a typed row (#1134: ``source`` manual, no ``external_id``, no ``sync_id``).

    The always-on block and every reader that dumped the whole profile keep
    their exact text for a profile that never filled the new parts.
    """
    dumped = profile.model_dump(mode="json", exclude=set(EXPOSURE_FIELDS) | set(PAGE_ONLY_FIELDS))
    for pa_row in dumped.get("primary_assets") or []:
        if not pa_row.get("asset_names"):
            pa_row.pop("asset_names", None)
    for entry in dumped.get("software") or []:
        if not entry.get("version"):
            entry.pop("version", None)
    for key in ("primary_assets", "software"):
        for row in dumped.get(key) or []:
            _drop_typed_provenance(row)
    if not dumped.get("fx_rates"):
        # #1383: a profile with no reference rate dumps as before.
        dumped.pop("fx_rates", None)
    if not dumped.get("pir_details"):
        # #1773: a profile with no PIR detail dumps as before.
        dumped.pop("pir_details", None)
    if not dumped.get("pir_min_scores"):
        # #2435: a profile with no per-PIR minimum score dumps as before.
        dumped.pop("pir_min_scores", None)
    if not dumped.get("shipped_products"):
        # #1432: a profile that ships nothing dumps as before.
        dumped.pop("shipped_products", None)
    ticketing = dumped.get("ticketing")
    if isinstance(ticketing, dict) and ticketing.get("sync") == TicketingSync().model_dump(mode="json"):
        ticketing.pop("sync", None)
    return dumped


def exposure_counts(profile: OrganizationProfile, asset_counts: dict[str, int] | None = None) -> dict[str, int]:
    """The size of each part #11543 added (the policy counts its explicit rules).

    *asset_counts* (:func:`asset_list_counts`) replaces the asset and group
    counts of the stored profile with the records' (#1543).
    """
    policy = profile.prioritization_policy
    defaults = PrioritizationPolicy()
    rules = sum(
        1
        for name in PrioritizationPolicy.model_fields
        if name not in ("exclusions", "accepted_risks") and getattr(policy, name) != getattr(defaults, name)
    )
    lists = asset_counts or {}
    return {
        "assets_exposure": lists.get("assets_exposure", len(profile.assets_exposure)),
        "asset_groups": lists.get("asset_groups", len(profile.asset_groups)),
        "prioritization_policy": rules + len(policy.exclusions) + len(policy.accepted_risks),
        "enterprise_knowledge": len(profile.enterprise_knowledge),
    }


def exposure_line(profile: OrganizationProfile, asset_counts: dict[str, int] | None = None) -> str:
    """One announcement line for the filled parts of #11543 (``""`` when none is): never their content."""
    counts = {name: count for name, count in exposure_counts(profile, asset_counts).items() if count}
    if not counts:
        return ""
    parts = ", ".join(f"{name} ({count})" for name, count in counts.items())
    return f"Also filled, read by tool: {parts}; read one with get_organization_context(category=NAME)."


def render_for_agent(
    row: OrganizationContextVersion | None,
    *,
    ctem_work: bool = True,
    asset_counts: dict[str, int] | None = None,
) -> str:
    """The profile as an agent reads it: the filled fields, then the missing ones.

    The missing fields are named so the run reports them instead of
    guessing; the profile is data a person typed, so it is quarantined.
    *ctem_work* false (:func:`ctem_work_for`): the CTEM-only fields are not
    named and no brief notice is asked (#1384). *asset_counts*
    (:func:`asset_list_counts`): the asset and group sizes the records hold,
    never the rows (#1543).
    """
    import json

    from app.services.content_safety import wrap_untrusted

    identity = identity_only()
    if row is None:
        every = missing_for_run([path for path, _label in contract_fields()], ctem_work=ctem_work)
        labels = dict(CONTRACT_FIELDS)
        return (
            "No organization profile is saved yet. Every field is missing: "
            + ", ".join(labels[path] for path in every)
            + ". Do not guess them: name the fields your work needed in your output, "
            "and say an administrator fills them in Settings > Organization."
            + (f" {brief_notice_instruction(every)}" if ctem_work else "")
        )
    profile = parse_profile(row.profile)
    missing = missing_for_run(missing_fields(profile), ctem_work=ctem_work)
    filled = {
        key: value
        for key, value in legacy_dump(profile).items()
        if value not in ("", [], None)
        and not (isinstance(value, dict) and not any(value.values()))
        and key != "onboarding_skipped"
        # Copilot mode: the identity alone reaches an agent (#1690).
        and (not identity or key in IDENTITY_FIELDS)
    }
    labels = dict(CONTRACT_FIELDS)
    exposure = "" if identity else exposure_line(profile, asset_counts)
    lines = [
        f"Organization profile, version {row.version} (saved {row.created_at:%Y-%m-%d %H:%M} UTC, source {row.source}).",
        "Cite the fields you used. Fields not filled: "
        + (", ".join(labels[path] for path in missing) if missing else "none")
        + "."
        + (" Do not guess them: name the ones your work needed in your output." if missing else "")
        + (f" {brief_notice_instruction(missing)}" if missing and ctem_work else ""),
        "",
        wrap_untrusted(json.dumps(filled, ensure_ascii=False, indent=1), "organization profile"),
    ]
    # #1773: the rate notice comes before the exposure line, so the block of a profile
    # without rows stays a prefix of the block of the same profile with rows.
    notice = "" if identity else rate_notice(profile)
    if notice:
        lines.append(notice)
    if exposure:
        lines.append(exposure)
    return "\n".join(lines)
