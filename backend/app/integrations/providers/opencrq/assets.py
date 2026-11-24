"""OpenCRQ asset inventory, dependency graph, and vulnerability tools.

* ``list_opencrq_primary_assets``    - business (primary) assets of an org.
* ``get_opencrq_primary_asset``      - every field of one primary asset.
* ``list_opencrq_business_value_categories`` - closed catalogue a primary
  asset is classified into.
* ``create/update_opencrq_primary_asset``, ``delete_opencrq_primary_assets``
* ``list_opencrq_supporting_assets`` - technical (supporting) assets, paged.
* ``get_opencrq_supporting_asset``   - supporting asset detail.
* ``create/update_opencrq_supporting_asset``,
  ``delete_opencrq_supporting_assets``,
  ``reset_opencrq_supporting_asset_to_connectors`` - back to the connectors' values.
* ``list/create/delete_opencrq_asset_dependencies`` - dependency links.
* ``update_opencrq_asset_dependency_roles`` - roles of support-to-primary links.
* ``create_opencrq_asset_evidence`` / ``update_opencrq_evidence_state`` /
  ``delete_opencrq_asset_evidence`` / ``reset_opencrq_evidence_state`` /
  ``get_opencrq_evidence`` / ``list_opencrq_cve_evidences`` - evidence.
* ``list_opencrq_unique_findings``  - the tenant's findings, one row per CVE or technique.
* ``search_opencrq_cves``            - CVE knowledge (CISA KEV / EPSS / CVSS).
* ``list_opencrq_cves`` / ``get_opencrq_cve`` - CVE knowledge base.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import (
    ASSET_CATEGORIES,
    ASSET_CRITICALITIES,
    ASSET_EXPOSURES,
    ASSET_SUBCATEGORIES,
    TENANT_PROP,
    _clean_ids,
    _client,
    _fmt_cve,
    _fmt_empty_page,
    _fmt_supporting_asset,
    _grc_primary_asset_link,
    _one_line,
    _quarantined,
    _resolve_tenant,
    _trpc,
    _untrusted,
)

logger = logging.getLogger(__name__)

_SOURCE = "integration:opencrq"

# A primary asset's native fields (OpenCRQ module/src/modules/primary-assets/constants.ts).
# The four security needs share one scale, lowest first: the overall level OpenCRQ shows is
# not stored, it is the highest of the criteria set.
SECURITY_NEED_LEVELS = ("low", "medium", "high", "very_high")
_SECURITY_NEED_CRITERIA = (
    ("availability", "Availability"),
    ("integrity", "Integrity"),
    ("confidentiality", "Confidentiality"),
    ("traceability", "Traceability"),
)
_REFERENCE_MAX_LENGTH = 64
_BUSINESS_FUNCTION_MAX_LENGTH = 128
# Ten years, OpenCRQ's PRIMARY_ASSET_DURATION_MAX_MINUTES.
_DURATION_MAX_MINUTES = 10 * 365 * 24 * 60
_DURATIONS = (("rtoMinutes", "RTO"), ("rpoMinutes", "RPO"), ("mtpdMinutes", "MTPD"))
# Stands for an argument the call did not pass: an update leaves that field unchanged,
# where an explicit null (or an empty string) clears it.
_KEEP: Any = object()


def _native_value(argument: str, value: Any, kind: str) -> tuple[Any, str | None]:
    """The value OpenCRQ takes for one native field of a primary asset, or the refusal.

    ``None`` and ``""`` are no value (sent as ``null``, which clears it on an update).
    *kind* is ``level`` (a security need), ``minutes`` (a duration) or ``text:<max length>``.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return None, None
    if kind == "level":
        level = str(value).strip().lower()
        if level not in SECURITY_NEED_LEVELS:
            return None, (
                f"{argument} must be one of: {', '.join(SECURITY_NEED_LEVELS)} (null or an empty string clears it)."
            )
        return level, None
    if kind == "minutes":
        minutes = value
        if isinstance(minutes, float) and minutes.is_integer():
            minutes = int(minutes)
        if isinstance(minutes, str) and minutes.strip().isdigit():
            minutes = int(minutes.strip())
        if isinstance(minutes, bool) or not isinstance(minutes, int) or not 0 <= minutes <= _DURATION_MAX_MINUTES:
            return None, (
                f"{argument} must be a whole number of minutes from 0 to {_DURATION_MAX_MINUTES} "
                "(10 years); null clears it."
            )
        return minutes, None
    limit = int(kind.split(":", 1)[1])
    text = str(value).strip()
    if len(text) > limit:
        return None, f"{argument} must be at most {limit} characters."
    return text, None


def _overall_security_need(asset: dict[str, Any]) -> str | None:
    """The highest of the four security needs set on *asset*, ``None`` when none is."""
    ranks = [
        SECURITY_NEED_LEVELS.index(asset[key])
        for key, _ in _SECURITY_NEED_CRITERIA
        if asset.get(key) in SECURITY_NEED_LEVELS
    ]
    return SECURITY_NEED_LEVELS[max(ranks)] if ranks else None


def _fmt_minutes(minutes: Any) -> str:
    """A duration OpenCRQ stores in minutes, with its round unit when it has one."""
    if not isinstance(minutes, int) or isinstance(minutes, bool):
        return "not set"
    if minutes and minutes % 1440 == 0:
        return f"{minutes} min ({minutes // 1440} d)"
    if minutes and minutes % 60 == 0:
        return f"{minutes} min ({minutes // 60} h)"
    return f"{minutes} min"


def _fmt_security_needs(asset: dict[str, Any]) -> str:
    """``A/I/C/T: high/high/very_high/medium (overall very_high, computed)``, on one line."""
    levels = "/".join(
        str(asset[key]) if asset.get(key) in SECURITY_NEED_LEVELS else "not set" for key, _ in _SECURITY_NEED_CRITERIA
    )
    overall = _overall_security_need(asset)
    return f"A/I/C/T: {levels} (overall {overall or 'not set'}, computed)"


def _fmt_durations(asset: dict[str, Any]) -> str:
    """``RTO 240 min (4 h), RPO not set, MTPD 1440 min (1 d)``."""
    return ", ".join(f"{label} {_fmt_minutes(asset.get(key))}" for key, label in _DURATIONS)


def _fmt_primary_asset(asset: dict[str, Any], base_url: str, tenant: str) -> str:
    """Every field of one primary asset as ``getPrimaryAsset`` answers it."""
    organization = asset.get("organization") or {}
    owner = asset.get("owner") or {}
    lines = [
        f"[PrimaryAsset] {_one_line(asset.get('name'), 200, 'unnamed')}",
        f"ID: {asset.get('id')}",
    ]
    if organization.get("name") or asset.get("organizationId"):
        lines.append(
            f"Organization: {_one_line(organization.get('name'), 200, 'unnamed')} "
            f"(id: {organization.get('id') or asset.get('organizationId')})"
        )
    lines.append(
        f"Owner: {_one_line(owner.get('name'), 200, 'unnamed')} (id: {owner.get('id')})"
        if owner.get("id")
        else "Owner: none"
    )
    lines.append(f"Reference: {_one_line(asset.get('reference'), _REFERENCE_MAX_LENGTH) or 'not set'}")
    lines.append(
        f"Business function: {_one_line(asset.get('businessFunction'), _BUSINESS_FUNCTION_MAX_LENGTH) or 'not set'}"
    )
    if asset.get("description"):
        lines.append(f"Description: {_one_line(asset['description'], 500)}")
    categories = ", ".join(
        _one_line(c.get("name"), 120) or _one_line(c.get("code"), 60)
        for c in asset.get("businessValueCategories") or []
    )
    lines.append(f"Business value categories: {categories or 'none'}")
    lines.append("Security needs (scale low < medium < high < very_high):")
    for key, label in _SECURITY_NEED_CRITERIA:
        value = asset.get(key)
        lines.append(f"  {label}: {value if value in SECURITY_NEED_LEVELS else 'not set'}")
    lines.append(f"  Overall level (computed, the highest criterion set): {_overall_security_need(asset) or 'not set'}")
    for key, label in _DURATIONS:
        lines.append(f"{label}: {_fmt_minutes(asset.get(key))}")
    link = _grc_primary_asset_link(
        base_url, tenant, organization.get("id") or asset.get("organizationId"), asset.get("id")
    )
    if link:
        lines.append(link)
    return "\n".join(lines)


_LINK_TYPES = ["support_to_primary", "support_to_support"]

_LINK_ROLES = [
    "stores",
    "processes",
    "reads",
    "transmits",
    "administers",
    "monitors",
    "supports_availability",
    "performs",
    "unknown",
]

_DISMISSAL_REASON_CODES = {
    "risk_accepted": [
        "wont_fix",
        "patch_unavailable",
        "downtime_not_permitted",
        "application_incompatibility",
        "compensatory_control_applied",
        "technical_debt_or_eol",
        "not_externally_exposed",
        "business_exception",
        "vendor_dependency",
        "other",
    ],
    "false_positive": ["detection_error", "not_applicable", "other"],
}
_REASON_MAX = 2000

_FINDING_SOURCES = ["first", "nist", "cisa", "microsoft_graph", "openaev", "drata", "user"]
_UNIQUE_FINDING_SORTS = ["score", "affectedAssets", "name", "age"]


# MAX_DEPENDENCY_LINKS_PER_WRITE (asset-dependencies/constants.ts), the .max() of the asset-dependencies write inputs.
_MAX_LINKS_PER_WRITE = 500


def _fmt_roles(link: dict) -> str:
    roles = link.get("roles") or []
    return f"roles {', '.join(_one_line(r, 40) for r in roles)}" if roles else "no role"


def _link_roles(roles: list[str] | None) -> tuple[list[str], str | None]:
    """Normalise a link role list; the second item is the refusal, if any."""
    link_roles = list(dict.fromkeys(r.strip().lower() for r in _clean_ids(roles)))
    if any(r not in _LINK_ROLES for r in link_roles):
        return link_roles, "roles must be among: " + ", ".join(_LINK_ROLES) + "."
    if "unknown" in link_roles and len(link_roles) > 1:
        return link_roles, "the unknown role cannot be combined with another role."
    return link_roles, None


_STRING = {"type": "string"}
_STRING_LIST = {"type": "array", "items": {"type": "string"}}
_BOOLEAN = {"type": "boolean"}

#: The hand-typed property block of each family (MANUAL_*_PROPERTIES_SCHEMA in
#: OpenCRQ's supporting-assets-router.ts), without the identifiers only a
#: connector assigns. Keys are snake_case here, camelCase on the wire.
_ASSET_PROPERTY_FIELDS: dict[str, dict[str, dict]] = {
    "machine": {
        "hostname": _STRING,
        "ip_addresses": _STRING_LIST,
        "mac_addresses": _STRING_LIST,
        "seen_ip": _STRING,
        "cloud_provider": {
            "type": "string",
            "enum": ["aws", "azure", "gcp", "oci", "alibaba", "kubernetes", "other"],
        },
        "cloud_native_type": _STRING,
        "cloud_region": _STRING,
        "platform": _STRING,
        "arch": {"type": "string", "enum": ["x86_64", "arm64"]},
        "vendor": _STRING,
        "model": _STRING,
        "is_eol": _BOOLEAN,
    },
    "identity": {
        "user_principal_name": _STRING,
        "account_domain": _STRING,
        "account_name": _STRING,
        "identity_provider": {"type": "string", "enum": ["entra", "active_directory"]},
        "department": _STRING,
        "job_title": _STRING,
        "is_enabled": _BOOLEAN,
        "assigned_roles": _STRING_LIST,
        "privileged_pim_roles": _STRING_LIST,
    },
    "ai_target": {
        "provider": {
            "type": "string",
            "enum": [
                "openai_compatible", "anthropic", "azure_openai", "aws_bedrock",
                "google_vertex", "huggingface", "ollama", "custom_http",
                "mcp_server", "agent_http", "xtm_one",
            ],
        },
        "endpoint_url": _STRING,
        "model": _STRING,
        "modality": {"type": "string", "enum": ["text", "vision", "audio", "multimodal"]},
        "system_prompt": _STRING,
    },
}

#: identity-families.ts: every category but these has no hand-typed block.
_ASSET_PROPERTY_FAMILY_OF = {
    **dict.fromkeys(
        (
            "host", "container_workload", "cloud_resource", "mobile_device",
            "network_device", "iot_ot_device",
        ),
        "machine",
    ),
    "identity": "identity",
    "ai_target": "ai_target",
}

_ASSET_PROPERTIES_SCHEMA = {
    "type": "object",
    "description": (
        "Hand-typed property block, one family per asset: machine (host, "
        "container_workload, cloud_resource, mobile_device, network_device, "
        "iot_ot_device), identity, or ai_target. The family follows the "
        "category, and a field of another family is refused. Sending a block "
        "replaces the whole hand-typed block: a field you leave out is "
        "emptied. web_application, saas_application, security_platform and "
        "generic_asset have no block."
    ),
    "properties": {
        "family": {
            "type": "string",
            "enum": list(_ASSET_PROPERTY_FIELDS),
            "description": "Optional; must be the family of the category.",
        },
        **{
            key: schema
            for fields in _ASSET_PROPERTY_FIELDS.values()
            for key, schema in fields.items()
        },
    },
    "additionalProperties": False,
}

_ASSET_SUBCATEGORY_VALUES = [s for subs in ASSET_SUBCATEGORIES.values() for s in subs]

_TAG_NAMES_DESCRIPTION = (
    "The complete list of hand-set tags: it replaces every tag set by hand "
    "(connector tags are untouched), so start from the tags "
    "get_opencrq_supporting_asset shows. An empty list removes them all."
)

_CONNECTOR_NOTE = (
    "Done with a service account, the values are not pinned and the next "
    "connector pass may overwrite them."
)

_MANUAL_VALUE_LABELS = {"linkedPerson": "linked person"}

#: ASSET_GROUP_REF_SCHEMA in OpenCRQ's supporting-assets-router.ts.
_GROUP_TEXT_MAX = 256

_GROUPS_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "external_reference": {"type": "string"},
        },
        "required": ["name"],
        "additionalProperties": False,
    },
    "description": (
        "The asset groups this caller puts the asset in, matched by external "
        "reference then name, created when none matches. Replaces only this "
        "caller's own memberships; an empty list removes them."
    ),
}

_OWNER_NAME_DESCRIPTION = (
    "The owner as text (an email or a display name): OpenCRQ resolves it to "
    "one tenant member and keeps it as the linked person. An owner set by a "
    "person is never replaced."
)

#: OwnerDecision report outcomes of OpenCRQ's owner-resolution.ts.
_OWNER_OUTCOMES = {
    "resolved": "set to the tenant member it names",
    "unresolved": "no tenant member matches it, the owner is unchanged",
    "ambiguous": "several tenant members match it, the owner is unchanged",
    "kept_manual": "the owner a person set is kept",
}


def _asset_groups(groups: object) -> tuple[list[dict[str, Any]] | None, str | None]:
    """The wire ``groups`` of a create or an update, or the refusal."""
    if not isinstance(groups, list):
        return None, "groups must be a list of objects with a name."
    out: list[dict[str, Any]] = []
    for group in groups:
        if not isinstance(group, dict):
            return None, "groups must be a list of objects with a name."
        name = str(group.get("name") or "").strip()
        reference = str(group.get("external_reference") or "").strip()
        if not name:
            return None, "every group needs a name."
        if len(name) > _GROUP_TEXT_MAX or len(reference) > _GROUP_TEXT_MAX:
            return None, f"a group name or external reference is at most {_GROUP_TEXT_MAX} characters."
        out.append({"name": name, "externalReference": reference or None})
    return out, None


def _owner_outcome(result: object) -> str:
    """The sentence for the owner OpenCRQ resolved from ``ownerName``, or nothing."""
    owner = result.get("owner") if isinstance(result, dict) else None
    if not isinstance(owner, dict):
        return ""
    outcome = _OWNER_OUTCOMES.get(str(owner.get("outcome") or ""))
    if not outcome:
        return ""
    return f" Owner '{_one_line(owner.get('name'), 120)}': {outcome}."


def _camel(key: str) -> str:
    head, *rest = key.split("_")
    return head + "".join(part.capitalize() for part in rest)


def _asset_properties_block(
    category: str, properties: object
) -> tuple[dict | None, str | None]:
    """The wire block of *properties* for *category*'s family, or the refusal."""
    family = _ASSET_PROPERTY_FAMILY_OF.get(category)
    if family is None:
        return None, (
            f"category '{category}' has no properties block (only machine, "
            "identity and ai_target categories take one)."
        )
    if not isinstance(properties, dict):
        return None, "properties must be an object."
    declared = properties.get("family")
    if declared is not None and declared != family:
        return None, (
            f"properties.family is '{declared}' but category '{category}' takes "
            f"a '{family}' block."
        )
    if not set(properties) - {"family"}:
        return None, "properties needs at least one field."
    allowed = _ASSET_PROPERTY_FIELDS[family]
    block: dict = {"family": family}
    for key, value in properties.items():
        if key == "family":
            continue
        if key not in allowed:
            owner = next(
                (f for f, fields in _ASSET_PROPERTY_FIELDS.items() if key in fields),
                None,
            )
            if owner:
                return None, (
                    f"properties.{key} belongs to the '{owner}' block, but "
                    f"category '{category}' takes a '{family}' block."
                )
            return None, (
                f"properties.{key} is not a field of the '{family}' block "
                f"({', '.join(allowed)})."
            )
        block[_camel(key)] = value
    return block, None


def _asset_classification(
    category: str, subcategory: str, properties: object
) -> tuple[dict | None, str | None]:
    """Check the subcategory and build the properties block against *category*."""
    if subcategory and subcategory not in ASSET_SUBCATEGORIES.get(category, ()):
        held = ASSET_SUBCATEGORIES.get(category, ())
        return None, (
            f"subcategory '{subcategory}' does not belong to category "
            f"'{category}'"
            + (f" (allowed: {', '.join(held)})." if held else ": it has none.")
        )
    if properties is None:
        return None, None
    return _asset_properties_block(category, properties)


def _latest_history_entry(history: list[dict]) -> dict:
    """The history entry OpenCRQ's ``resetEvidenceState`` checks, whatever order *history* comes in.

    OpenCRQ reads it as the latest ``stateChangedAt``, the larger ``id`` breaking a tie. Both are
    compared as served: a fixed-width ISO-8601 UTC instant (a JSON-encoded JavaScript Date) and a
    UUIDv7, whose text orders as their value does. A missing value ranks below any present one.
    """
    return max(
        history,
        key=lambda h: (str(h.get("stateChangedAt") or ""), str(h.get("id") or "")),
    )


def _evidence_reset_refusal(evidence_id: str, detail: dict) -> str | None:
    """Why OpenCRQ would answer an unmapped 500 to this reset, or None when it would accept it."""
    nothing_changed = " Nothing was changed."
    if not any(s.get("connectorId") for s in detail.get("sources") or []):
        return (
            f"Evidence {evidence_id} has no live connector source (attached by "
            "hand, or its connector was deleted), so there is no connector to "
            "hand it back to." + nothing_changed
        )
    history = detail.get("history") or []
    if not history:
        return f"Evidence {evidence_id} has no history, so there is no state to hand back." + nothing_changed
    origin = _latest_history_entry(history).get("origin")
    if origin != "user":
        return (
            f"The latest state of evidence {evidence_id} was not set by a person "
            f"(origin: {origin or 'unknown'}), so there is nothing to hand back."
            + nothing_changed
        )
    return None


def register(
    base_url: str, headers: dict[str, str], executor: CallableToolExecutor
) -> list[ToolDefinition]:
    async def list_opencrq_primary_assets(
        organization_id: str = "", tenant_id: str = ""
    ) -> str:
        if not organization_id:
            return (
                "organization_id is required "
                "(use list_opencrq_organizations to discover)."
            )
        tenant, err = await _resolve_tenant(base_url, headers, tenant_id)
        if err:
            return err
        assets, err = await _trpc(
            base_url, headers, "primaryAssets.getPrimaryAssets",
            "list primary assets",
            input={"organizationId": organization_id}, tenant_id=tenant,
        )
        if err:
            return err
        assets = assets or []
        if not assets:
            return "No primary assets found for this organization."
        lines = []
        for a in assets[:40]:
            entry = f"- {_one_line(a.get('name'), 200, 'unnamed')} (id: {a.get('id')})"
            categories = ", ".join(
                _one_line(c.get("name"), 120) or _one_line(c.get("code"), 60)
                for c in a.get("businessValueCategories") or []
            )
            if categories:
                entry += f" [{categories}]"
            if a.get("reference"):
                entry += f" ref {_one_line(a['reference'], _REFERENCE_MAX_LENGTH)}"
            if a.get("description"):
                entry += f" - {_one_line(a['description'], 120)}"
            entry += f"\n  {_fmt_security_needs(a)}; {_fmt_durations(a)}"
            if a.get("businessFunction"):
                entry += f"\n  Business function: {_one_line(a['businessFunction'], _BUSINESS_FUNCTION_MAX_LENGTH)}"
            lines.append(entry)
        return _quarantined(
            f"Primary assets ({len(assets)}):",
            "\n".join(lines),
            "primary assets",
            f"... and {len(assets) - 40} more." if len(assets) > 40 else "",
        )

    async def list_opencrq_business_value_categories(tenant_id: str = "") -> str:
        tenant, err = await _resolve_tenant(base_url, headers, tenant_id)
        if err:
            return err
        categories, err = await _trpc(
            base_url, headers, "businessValueCategories.getBusinessValueCategories",
            "list business value categories", tenant_id=tenant,
        )
        if err:
            return err
        categories = categories or []
        if not categories:
            return "No business value categories found."
        lines = []
        for c in categories:
            entry = f"- {_one_line(c.get('name'), 200, 'unnamed')} ({_one_line(c.get('code'), 60)}, id: {c.get('id')})"
            if c.get("description"):
                entry += f" - {_one_line(c['description'], 160)}"
            lines.append(entry)
        return _quarantined(
            f"Business value categories ({len(categories)}):", "\n".join(lines), "business value categories"
        )

    async def list_opencrq_supporting_assets(
        search: str = "", page: int = 1, limit: int = 25, tenant_id: str = ""
    ) -> str:
        if page < 1:
            return "page must be >= 1."
        if not 1 <= limit <= 500:
            return "limit must be between 1 and 500."
        tenant, err = await _resolve_tenant(base_url, headers, tenant_id)
        if err:
            return err
        payload: dict = {"page": page, "limit": limit, "sortDesc": False}
        if (search or "").strip():
            payload["search"] = search.strip()
        result, err = await _trpc(
            base_url, headers, "supportingAssets.listSupportingAssets",
            "list supporting assets", input=payload, tenant_id=tenant,
        )
        if err:
            return err
        assets = (result or {}).get("items") or []
        total = (result or {}).get("total", len(assets))
        if not assets:
            return _fmt_empty_page("No supporting assets matched.", page, total)
        return _quarantined(
            f"Supporting assets (page {page}, showing {len(assets)} of {total}):",
            "\n\n".join(_fmt_supporting_asset(a, base_url, tenant) for a in assets),
            "supporting assets (connector inventories and user edits)",
        )

    async def get_opencrq_supporting_asset(
        supporting_asset_id: str = "", tenant_id: str = ""
    ) -> str:
        if not supporting_asset_id:
            return "supporting_asset_id is required."
        tenant, err = await _resolve_tenant(base_url, headers, tenant_id)
        if err:
            return err
        asset, err = await _trpc(
            base_url, headers, "supportingAssets.getSupportingAsset",
            "get supporting asset",
            input={"id": supporting_asset_id}, tenant_id=tenant,
        )
        if err:
            return err
        if not asset:
            return f"Supporting asset {supporting_asset_id} not found."
        return _untrusted(
            _fmt_supporting_asset(asset, base_url, tenant),
            "supporting asset (connector inventories and user edits)",
        )

    async def get_opencrq_primary_asset(primary_asset_id: str = "", tenant_id: str = "") -> str:
        if not primary_asset_id:
            return "primary_asset_id is required (use list_opencrq_primary_assets to discover)."
        tenant, err = await _resolve_tenant(base_url, headers, tenant_id)
        if err or tenant is None:
            return err or "No OpenCRQ tenant resolved."
        asset, err = await _trpc(
            base_url,
            headers,
            "primaryAssets.getPrimaryAsset",
            "get primary asset",
            input={"id": primary_asset_id},
            tenant_id=tenant,
        )
        if err:
            return err
        if not asset:
            return f"Primary asset {primary_asset_id} not found."
        return _untrusted(_fmt_primary_asset(asset, base_url, tenant), "primary asset")

    async def search_opencrq_cves(query: str = "", tenant_id: str = "") -> str:
        query = (query or "").strip()
        if len(query) < 3:
            return "query must be at least 3 characters (e.g. 'CVE-2024')."
        tenant, err = await _resolve_tenant(base_url, headers, tenant_id)
        if err:
            return err
        results, err = await _trpc(
            base_url, headers, "cve.search", "search CVEs",
            input={"query": query[:100]}, tenant_id=tenant,
        )
        if err:
            return err
        results = results or []
        if not results:
            return f"No CVEs matched '{query}'."
        shown = results[:15]
        return _quarantined(
            f"CVEs ({len(results)}, showing {len(shown)}):",
            "\n\n".join(_fmt_cve(c, base_url, tenant) for c in shown),
            "CVE knowledge base (NIST, CISA KEV and EPSS feeds)",
        )

    async def create_opencrq_primary_asset(
        organization_id: str = "",
        name: str = "",
        description: str = "",
        owner_user_id: str = "",
        business_value_category_ids: list[str] | None = None,
        reference: str | None = None,
        business_function: str | None = None,
        availability: str | None = None,
        integrity: str | None = None,
        confidentiality: str | None = None,
        traceability: str | None = None,
        rto_minutes: int | None = None,
        rpo_minutes: int | None = None,
        mtpd_minutes: int | None = None,
        tenant_id: str = "",
    ) -> str:
        if not organization_id or not name:
            return "organization_id and name are required."
        category_ids = _clean_ids(business_value_category_ids)
        if not category_ids:
            return (
                "business_value_category_ids is required (one or more UUIDs, "
                "use list_opencrq_business_value_categories to discover)."
            )
        asset_id = str(uuid.uuid4())
        payload: dict[str, Any] = {
            "id": asset_id,
            "organizationId": organization_id,
            "name": name,
            "description": description or None,
            "ownerId": owner_user_id or None,
            "businessValueCategoryIds": category_ids,
        }
        for argument, key, kind, value in (
            ("reference", "reference", f"text:{_REFERENCE_MAX_LENGTH}", reference),
            ("business_function", "businessFunction", f"text:{_BUSINESS_FUNCTION_MAX_LENGTH}", business_function),
            ("availability", "availability", "level", availability),
            ("integrity", "integrity", "level", integrity),
            ("confidentiality", "confidentiality", "level", confidentiality),
            ("traceability", "traceability", "level", traceability),
            ("rto_minutes", "rtoMinutes", "minutes", rto_minutes),
            ("rpo_minutes", "rpoMinutes", "minutes", rpo_minutes),
            ("mtpd_minutes", "mtpdMinutes", "minutes", mtpd_minutes),
        ):
            native, refusal = _native_value(argument, value, kind)
            if refusal:
                return refusal
            if native is not None:
                payload[key] = native
        tenant, err = await _resolve_tenant(base_url, headers, tenant_id)
        if err:
            return err
        _, err = await _trpc(
            base_url, headers, "primaryAssets.createPrimaryAsset",
            "create primary asset",
            input=payload,
            mutation=True, tenant_id=tenant,
        )
        if err:
            return err
        out = f"Created primary asset '{name}' (id: {asset_id})."
        link = _grc_primary_asset_link(base_url, tenant, organization_id, asset_id)
        return out + (f"\n{link}" if link else "")

    async def update_opencrq_primary_asset(
        primary_asset_id: str = "",
        name: str = "",
        description: str | None = None,
        owner_user_id: str | None = None,
        business_value_category_ids: list[str] | None = None,
        reference: Any = _KEEP,
        business_function: Any = _KEEP,
        availability: Any = _KEEP,
        integrity: Any = _KEEP,
        confidentiality: Any = _KEEP,
        traceability: Any = _KEEP,
        rto_minutes: Any = _KEEP,
        rpo_minutes: Any = _KEEP,
        mtpd_minutes: Any = _KEEP,
        tenant_id: str = "",
    ) -> str:
        if not primary_asset_id:
            return "primary_asset_id is required."
        payload: dict = {"id": primary_asset_id}
        # An argument not passed keeps the field; null (or "") clears it.
        for argument, key, kind, value in (
            ("reference", "reference", f"text:{_REFERENCE_MAX_LENGTH}", reference),
            ("business_function", "businessFunction", f"text:{_BUSINESS_FUNCTION_MAX_LENGTH}", business_function),
            ("availability", "availability", "level", availability),
            ("integrity", "integrity", "level", integrity),
            ("confidentiality", "confidentiality", "level", confidentiality),
            ("traceability", "traceability", "level", traceability),
            ("rto_minutes", "rtoMinutes", "minutes", rto_minutes),
            ("rpo_minutes", "rpoMinutes", "minutes", rpo_minutes),
            ("mtpd_minutes", "mtpdMinutes", "minutes", mtpd_minutes),
        ):
            if value is _KEEP:
                continue
            native, refusal = _native_value(argument, value, kind)
            if refusal:
                return refusal
            payload[key] = native
        if name:
            payload["name"] = name
        if description is not None:
            payload["description"] = description or None
        if owner_user_id is not None:
            payload["ownerId"] = owner_user_id or None
        if business_value_category_ids is not None:
            category_ids = _clean_ids(business_value_category_ids)
            if not category_ids:
                return "business_value_category_ids needs at least one UUID."
            payload["businessValueCategoryIds"] = category_ids
        if len(payload) == 1:
            return "Provide at least one field to update."
        tenant, err = await _resolve_tenant(base_url, headers, tenant_id)
        if err:
            return err
        _, err = await _trpc(
            base_url, headers, "primaryAssets.updatePrimaryAsset",
            "update primary asset", input=payload, mutation=True,
            tenant_id=tenant,
        )
        if err:
            return err
        return f"Updated primary asset {primary_asset_id}."

    async def delete_opencrq_primary_assets(
        primary_asset_ids: list[str] | None = None, tenant_id: str = ""
    ) -> str:
        ids = _clean_ids(primary_asset_ids)
        if not ids:
            return "primary_asset_ids is required (one or more asset UUIDs)."
        tenant, err = await _resolve_tenant(base_url, headers, tenant_id)
        if err:
            return err
        _, err = await _trpc(
            base_url, headers, "primaryAssets.deletePrimaryAssets",
            "delete primary assets", input=ids, mutation=True, tenant_id=tenant,
        )
        if err:
            return err
        return f"Deleted {len(ids)} primary asset(s)."

    async def create_opencrq_supporting_asset(
        name: str = "",
        category: str = "",
        exposure: str = "",
        description: str = "",
        owner_user_id: str = "",
        criticality: str = "",
        subcategory: str = "",
        url: str = "",
        linked_person: str = "",
        tag_names: list[str] | None = None,
        properties: dict | None = None,
        organization_id: str = "",
        owner_name: str = "",
        groups: list[dict[str, Any]] | None = None,
        tenant_id: str = "",
    ) -> str:
        if not name:
            return "name is required."
        category = (category or "").strip().lower()
        if category not in ASSET_CATEGORIES:
            return "category must be one of: " + ", ".join(ASSET_CATEGORIES) + "."
        exposure = (exposure or "").strip().lower() or "unknown"
        if exposure not in ASSET_EXPOSURES:
            return "exposure must be one of: " + ", ".join(ASSET_EXPOSURES) + "."
        criticality = (criticality or "").strip().lower()
        if criticality and criticality not in ASSET_CRITICALITIES:
            return "criticality must be one of: " + ", ".join(ASSET_CRITICALITIES) + "."
        subcategory = (subcategory or "").strip().lower()
        block, err = _asset_classification(category, subcategory, properties)
        if err:
            return err
        wire_groups, err = _asset_groups(groups) if groups is not None else (None, None)
        if err:
            return err
        tenant, err = await _resolve_tenant(base_url, headers, tenant_id)
        if err:
            return err
        asset_id = str(uuid.uuid4())
        payload: dict = {
            "id": asset_id,
            "name": name,
            "description": description or None,
            "ownerId": owner_user_id or None,
            "exposure": exposure,
            "categoryId": category,
        }
        if criticality:
            payload["criticality"] = criticality
        if subcategory:
            payload["subcategoryId"] = subcategory
        if url:
            payload["url"] = url
        if linked_person:
            payload["linkedPerson"] = linked_person
        if tag_names:
            payload["tagNames"] = _clean_ids(tag_names)
        if block:
            payload["properties"] = block
        if organization_id:
            payload["organizationId"] = organization_id
        if owner_name and owner_name.strip():
            payload["ownerName"] = owner_name.strip()
        if wire_groups:
            payload["groups"] = wire_groups
        result, err = await _trpc(
            base_url, headers, "supportingAssets.createSupportingAsset",
            "create supporting asset", input=payload, mutation=True,
            tenant_id=tenant,
        )
        if err:
            return err
        return f"Created supporting asset '{name}' (id: {asset_id})." + _owner_outcome(result)

    async def update_opencrq_supporting_asset(
        supporting_asset_id: str = "",
        name: str = "",
        category: str = "",
        exposure: str = "",
        description: str | None = None,
        owner_user_id: str | None = None,
        criticality: str = "",
        subcategory: str | None = None,
        url: str | None = None,
        linked_person: str | None = None,
        tag_names: list[str] | None = None,
        properties: dict | None = None,
        owner_name: str | None = None,
        groups: list[dict[str, Any]] | None = None,
        tenant_id: str = "",
    ) -> str:
        if not supporting_asset_id:
            return "supporting_asset_id is required."
        payload: dict = {"id": supporting_asset_id}
        if name:
            payload["name"] = name
        category = (category or "").strip().lower()
        if category:
            if category not in ASSET_CATEGORIES:
                return (
                    "category must be one of: " + ", ".join(ASSET_CATEGORIES) + "."
                )
            payload["categoryId"] = category
        exposure = (exposure or "").strip().lower()
        if exposure:
            if exposure not in ASSET_EXPOSURES:
                return (
                    "exposure must be one of: " + ", ".join(ASSET_EXPOSURES) + "."
                )
            payload["exposure"] = exposure
        if description is not None:
            payload["description"] = description or None
        if owner_user_id is not None:
            payload["ownerId"] = owner_user_id or None
        criticality = (criticality or "").strip().lower()
        if criticality:
            if criticality not in ASSET_CRITICALITIES:
                return (
                    "criticality must be one of: " + ", ".join(ASSET_CRITICALITIES) + "."
                )
            payload["criticality"] = criticality
        if subcategory is not None:
            subcategory = subcategory.strip().lower()
            payload["subcategoryId"] = subcategory or None
        if url is not None:
            payload["url"] = url or None
        if linked_person is not None:
            payload["linkedPerson"] = linked_person or None
        if tag_names is not None:
            payload["tagNames"] = _clean_ids(tag_names)
        if owner_name and owner_name.strip():
            payload["ownerName"] = owner_name.strip()
        if groups is not None:
            wire_groups, err = _asset_groups(groups)
            if err:
                return err
            payload["groups"] = wire_groups
        if category:
            block, err = _asset_classification(category, subcategory or "", properties)
            if err:
                return err
            if block:
                payload["properties"] = block
        # Without a category the pair and the family depend on the asset's own.
        needs_asset = not category and bool(subcategory or properties is not None)
        if len(payload) == 1 and not needs_asset:
            return "Provide at least one field to update."
        async with _client(headers, timeout=30) as client:
            tenant, err = await _resolve_tenant(
                base_url, headers, tenant_id, client=client
            )
            if err:
                return err
            if needs_asset:
                asset, err = await _trpc(
                    base_url, headers, "supportingAssets.getSupportingAsset",
                    "get supporting asset", input={"id": supporting_asset_id},
                    tenant_id=tenant, client=client,
                )
                if err:
                    return err
                if not asset:
                    return f"Supporting asset {supporting_asset_id} not found."
                block, err = _asset_classification(
                    asset.get("categoryId") or "", subcategory or "", properties
                )
                if err:
                    return err
                if block:
                    payload["properties"] = block
            result, err = await _trpc(
                base_url, headers, "supportingAssets.updateSupportingAsset",
                "update supporting asset", input=payload, mutation=True,
                tenant_id=tenant, client=client,
            )
        if err:
            return err
        return f"Updated supporting asset {supporting_asset_id}." + _owner_outcome(result)

    async def reset_opencrq_supporting_asset_to_connectors(
        supporting_asset_id: str = "", tenant_id: str = ""
    ) -> str:
        if not supporting_asset_id:
            return "supporting_asset_id is required."
        async with _client(headers, timeout=30) as client:
            tenant, err = await _resolve_tenant(
                base_url, headers, tenant_id, client=client
            )
            if err:
                return err
            asset, err = await _trpc(
                base_url, headers, "supportingAssets.getSupportingAsset",
                "get supporting asset", input={"id": supporting_asset_id},
                tenant_id=tenant, client=client,
            )
            if err:
                return err
            if not asset:
                return f"Supporting asset {supporting_asset_id} not found."
            # A source whose connector was deleted keeps its row with no connector id.
            connectors = sorted({
                s.get("source") or "unknown source"
                for s in asset.get("sources") or []
                if s.get("connectorId")
            })
            if not connectors:
                return (
                    f"Nothing to resync: no live connector observes supporting "
                    f"asset {supporting_asset_id} (created by hand, or its "
                    "connector was deleted), so it was left untouched."
                )
            _, err = await _trpc(
                base_url, headers,
                "supportingAssets.resyncSupportingAssetWithConnectors",
                "resync supporting asset with connectors",
                input={"id": supporting_asset_id}, mutation=True,
                tenant_id=tenant, client=client,
            )
        if err:
            return err
        reported = ", ".join(connectors)
        # The report says what the read before the reset listed, never what the reset did:
        # OpenCRQ answers the mutation with nothing. A read missing either key says nothing
        # about what was set by hand, so it is not read as "nothing was".
        if "manualValues" not in asset or "manualProperties" not in asset:
            return (
                f"Reset supporting asset {supporting_asset_id} with {reported}. "
                "OpenCRQ's read of the asset did not list its hand-set values, so "
                "which of them went back to what the connectors report is not known. "
                "Tags and owner were kept."
            )
        hand_set = [
            _MANUAL_VALUE_LABELS.get(v["attribute"], v["attribute"])
            for v in asset.get("manualValues") or []
            if isinstance(v, dict) and v.get("attribute")
        ]
        if asset.get("manualProperties"):
            hand_set.append("property block")
        if not hand_set:
            return (
                f"Resynced supporting asset {supporting_asset_id} with "
                f"{reported}: OpenCRQ listed no hand-set value before the reset. "
                "Tags and owner were kept."
            )
        return (
            f"Reset supporting asset {supporting_asset_id}: "
            f"{', '.join(hand_set)} went back to what {reported} report. "
            "Tags and owner were kept."
        )

    async def delete_opencrq_supporting_assets(
        supporting_asset_ids: list[str] | None = None, tenant_id: str = ""
    ) -> str:
        ids = _clean_ids(supporting_asset_ids)
        if not ids:
            return "supporting_asset_ids is required (one or more asset UUIDs)."
        tenant, err = await _resolve_tenant(base_url, headers, tenant_id)
        if err:
            return err
        _, err = await _trpc(
            base_url, headers, "supportingAssets.deleteSupportingAssets",
            "delete supporting assets", input=ids, mutation=True,
            tenant_id=tenant,
        )
        if err:
            return err
        return f"Deleted {len(ids)} supporting asset(s)."

    async def list_opencrq_asset_dependencies(
        supporting_asset_id: str = "",
        primary_asset_id: str = "",
        tenant_id: str = "",
    ) -> str:
        if not supporting_asset_id and not primary_asset_id:
            return "Provide supporting_asset_id and/or primary_asset_id."
        # One client for the whole multi-call handler (tenant resolution +
        # up to 3 link queries) so the calls share a connection pool.
        async with _client(headers, timeout=30) as client:
            tenant, err = await _resolve_tenant(
                base_url, headers, tenant_id, client=client
            )
            if err:
                return err
            lines: list[str] = []
            notes: list[str] = []
            if supporting_asset_id:
                s2p, s2p_err = await _trpc(
                    base_url, headers,
                    "assetDependencies.getSupportToPrimaryLinksBySupportingAssetId",
                    "list support-to-primary links",
                    input={"supportingAssetId": supporting_asset_id},
                    tenant_id=tenant, client=client,
                )
                s2s, s2s_err = await _trpc(
                    base_url, headers,
                    "assetDependencies.getSupportToSupportLinks",
                    "list support-to-support links",
                    input={"assetId": supporting_asset_id},
                    tenant_id=tenant, client=client,
                )
                if s2p_err:
                    notes.append(f"Support->primary: lookup failed - {s2p_err}")
                else:
                    lines.append(f"  Support->primary ({len(s2p or [])}):")
                    for x in (s2p or [])[:30]:
                        lines.append(
                            f"    - link {x.get('id')}: primary asset "
                            f"{_one_line(x.get('primaryAssetName'), 200) or x.get('primaryAssetId')}"
                            + (
                                f" ({_one_line(x['organizationName'], 200)})"
                                if x.get("organizationName")
                                else ""
                            )
                            + f", {_fmt_roles(x)} "
                            f"(primary asset id: {x.get('primaryAssetId')})"
                        )
                    if len(s2p or []) > 30:
                        lines.append(f"    ... and {len(s2p) - 30} more.")
                if s2s_err:
                    notes.append(f"Support->support: lookup failed - {s2s_err}")
                else:
                    lines.append(f"  Support->support ({len(s2s or [])}):")
                    for x in (s2s or [])[:30]:
                        related = (
                            _one_line(x.get("relatedAssetName"), 200)
                            or _one_line(x.get("relatedAssetDerivedName"), 200)
                            or x.get("relatedAssetId")
                        )
                        verb = (
                            "provides to"
                            if x.get("direction") == "provides"
                            else "depends on"
                        )
                        lines.append(
                            f"    - link {x.get('id')}: {verb} {related} "
                            f"(asset id: {x.get('relatedAssetId')})"
                        )
                    if len(s2s or []) > 30:
                        lines.append(f"    ... and {len(s2s) - 30} more.")
            if primary_asset_id:
                p_links, p_err = await _trpc(
                    base_url, headers,
                    "assetDependencies.getSupportToPrimaryLinksByPrimaryAssetId",
                    "list primary asset links",
                    input={"primaryAssetId": primary_asset_id},
                    tenant_id=tenant, client=client,
                )
                if p_err:
                    notes.append(f"Primary asset links: lookup failed - {p_err}")
                else:
                    lines.append(f"  Primary asset links ({len(p_links or [])}):")
                    for x in (p_links or [])[:30]:
                        name = (
                            _one_line(x.get("supportingAssetName"), 200)
                            or _one_line(x.get("supportingAssetDerivedName"), 200)
                            or x.get("supportingAssetId")
                        )
                        lines.append(
                            f"    - link {x.get('id')}: supporting asset {name}"
                            + (
                                f" ({_one_line(x['supportingAssetCategoryId'], 60)})"
                                if x.get("supportingAssetCategoryId")
                                else ""
                            )
                            + f", {_fmt_roles(x)} "
                            f"(supporting asset id: {x.get('supportingAssetId')})"
                        )
                    if len(p_links or []) > 30:
                        lines.append(f"    ... and {len(p_links) - 30} more.")
        return _quarantined(
            "Asset dependency links:", "\n".join(lines), "asset dependency links (asset names)", *notes
        )

    async def create_opencrq_asset_dependency(
        link_type: str = "",
        supporting_asset_id: str = "",
        primary_asset_id: str = "",
        provider_asset_id: str = "",
        dependent_asset_id: str = "",
        roles: list[str] | None = None,
        tenant_id: str = "",
    ) -> str:
        link_type = (link_type or "").strip().lower()
        if link_type not in ("support_to_primary", "support_to_support"):
            return "link_type must be support_to_primary or support_to_support."
        link_roles, roles_error = _link_roles(roles)
        if link_roles and link_type == "support_to_support":
            return "roles apply to support_to_primary links only."
        if roles_error:
            return roles_error
        link_id = str(uuid.uuid4())
        if link_type == "support_to_primary":
            if not supporting_asset_id or not primary_asset_id:
                return (
                    "supporting_asset_id and primary_asset_id are required "
                    "for support_to_primary links."
                )
            procedure = "assetDependencies.createSupportToPrimaryLinks"
            link: dict = {
                "id": link_id,
                "supportingAssetId": supporting_asset_id,
                "primaryAssetId": primary_asset_id,
            }
            if link_roles:
                link["roles"] = link_roles
        else:
            if not provider_asset_id or not dependent_asset_id:
                return (
                    "provider_asset_id and dependent_asset_id are required "
                    "for support_to_support links."
                )
            if provider_asset_id == dependent_asset_id:
                return "an asset cannot depend on itself."
            procedure = "assetDependencies.createSupportToSupportLinks"
            link = {
                "id": link_id,
                "providerAssetId": provider_asset_id,
                "dependentAssetId": dependent_asset_id,
            }
        tenant, err = await _resolve_tenant(base_url, headers, tenant_id)
        if err:
            return err
        outcome, err = await _trpc(
            base_url, headers, procedure, "create asset dependency",
            input=[link], mutation=True, tenant_id=tenant,
        )
        if err:
            return err
        if link_id in ((outcome or {}).get("goneIds") or []):
            return (
                "Error: OpenCRQ could not reach one of the assets (deleted or "
                "not visible); no link was created."
            )
        if link_id in ((outcome or {}).get("skippedIds") or []):
            return (
                "No link created: these assets are already linked and the "
                "existing link keeps its roles."
            )
        return f"Created {link_type} dependency link (id: {link_id})."

    async def update_opencrq_asset_dependency_roles(
        link_ids: list[str] | None = None,
        roles: list[str] | None = None,
        tenant_id: str = "",
    ) -> str:
        ids = list(dict.fromkeys(_clean_ids(link_ids)))
        if not ids:
            return "link_ids is required (one or more support_to_primary link UUIDs)."
        if len(ids) > _MAX_LINKS_PER_WRITE:
            return f"link_ids takes at most {_MAX_LINKS_PER_WRITE} links per call."
        link_roles, roles_error = _link_roles(roles)
        if roles_error:
            return roles_error
        if not link_roles:
            return "roles is required: give at least one role."
        tenant, err = await _resolve_tenant(base_url, headers, tenant_id)
        if err:
            return err
        outcome, err = await _trpc(
            base_url, headers, "assetDependencies.updateSupportToPrimaryLinks",
            "update asset dependency roles",
            input=[{"id": link_id, "roles": link_roles} for link_id in ids],
            mutation=True, tenant_id=tenant,
        )
        if err:
            return err
        skipped = [i for i in ((outcome or {}).get("skippedIds") or []) if i in ids]
        out = (
            f"Updated {len(ids) - len(skipped)} support_to_primary dependency "
            f"link(s) to roles {', '.join(link_roles)}."
        )
        if skipped:
            out += (
                f" Skipped {len(skipped)} gone link(s) (deleted or not "
                f"visible): {', '.join(skipped)}."
            )
        return out

    async def delete_opencrq_asset_dependencies(
        link_type: str = "",
        link_ids: list[str] | None = None,
        tenant_id: str = "",
    ) -> str:
        link_type = (link_type or "").strip().lower()
        if link_type not in ("support_to_primary", "support_to_support"):
            return "link_type must be support_to_primary or support_to_support."
        ids = _clean_ids(link_ids)
        if not ids:
            return "link_ids is required (one or more dependency link UUIDs)."
        procedure = (
            "assetDependencies.deleteSupportToPrimaryLinks"
            if link_type == "support_to_primary"
            else "assetDependencies.deleteSupportToSupportLinks"
        )
        tenant, err = await _resolve_tenant(base_url, headers, tenant_id)
        if err:
            return err
        _, err = await _trpc(
            base_url, headers, procedure, "delete asset dependencies",
            input=ids, mutation=True, tenant_id=tenant,
        )
        if err:
            return err
        return f"Deleted {len(ids)} {link_type} dependency link(s)."

    async def create_opencrq_asset_evidence(
        supporting_asset_id: str = "", cve_record_id: str = "", tenant_id: str = ""
    ) -> str:
        if not supporting_asset_id or not cve_record_id:
            return "supporting_asset_id and cve_record_id are required."
        tenant, err = await _resolve_tenant(base_url, headers, tenant_id)
        if err:
            return err
        created, err = await _trpc(
            base_url, headers, "supportingAssets.createAssetEvidence",
            "create asset evidence",
            input={"supportingAssetId": supporting_asset_id, "cveId": cve_record_id},
            mutation=True, tenant_id=tenant,
        )
        if err:
            return err
        evidence_id = (created or {}).get("id")
        return (
            f"Attached CVE evidence {cve_record_id} to supporting asset "
            f"{supporting_asset_id}"
            + (f" (evidence id: {evidence_id})." if evidence_id else ".")
        )

    async def update_opencrq_evidence_state(
        evidence_id: str = "",
        state: str = "",
        dismissal_type: str = "",
        reason_code: str = "",
        reason: str = "",
        tenant_id: str = "",
    ) -> str:
        if not evidence_id:
            return "evidence_id is required."
        state = (state or "").strip().lower()
        if state not in ("applicable", "dismissed"):
            return "state must be applicable or dismissed."
        dismissal_type = (dismissal_type or "").strip().lower()
        reason_code = (reason_code or "").strip().lower()
        reason = (reason or "").strip()
        payload: dict = {"evidenceId": evidence_id, "state": state}
        if state == "dismissed":
            if dismissal_type not in _DISMISSAL_REASON_CODES:
                return (
                    "dismissal_type is required when dismissing evidence: "
                    + ", ".join(_DISMISSAL_REASON_CODES) + "."
                )
            if reason_code not in _DISMISSAL_REASON_CODES[dismissal_type]:
                return (
                    f"reason_code for {dismissal_type} must be one of: "
                    + ", ".join(_DISMISSAL_REASON_CODES[dismissal_type]) + "."
                )
            if not reason:
                return "reason is required when dismissing evidence."
            if len(reason) > _REASON_MAX:
                return f"reason must be at most {_REASON_MAX} characters."
            payload.update(
                {
                    "dismissalType": dismissal_type,
                    "reasonCode": reason_code,
                    "reason": reason,
                }
            )
        elif dismissal_type or reason_code or reason:
            return (
                "dismissal_type, reason_code and reason apply only to "
                "state=dismissed."
            )
        tenant, err = await _resolve_tenant(base_url, headers, tenant_id)
        if err:
            return err
        _, err = await _trpc(
            base_url, headers, "supportingAssets.updateEvidenceState",
            "update evidence state", input=payload, mutation=True,
            tenant_id=tenant,
        )
        if err:
            return err
        return f"Marked evidence {evidence_id} as {state}."

    async def delete_opencrq_asset_evidence(
        evidence_id: str = "", tenant_id: str = ""
    ) -> str:
        if not evidence_id:
            return "evidence_id is required."
        tenant, err = await _resolve_tenant(base_url, headers, tenant_id)
        if err:
            return err
        _, err = await _trpc(
            base_url, headers, "supportingAssets.deleteAssetEvidence",
            "delete asset evidence", input={"evidenceId": evidence_id},
            mutation=True, tenant_id=tenant,
        )
        if err:
            return err
        return f"Deleted evidence {evidence_id}."

    async def reset_opencrq_evidence_state(
        evidence_id: str = "", tenant_id: str = ""
    ) -> str:
        if not evidence_id:
            return "evidence_id is required."
        async with _client(headers, timeout=30) as client:
            tenant, err = await _resolve_tenant(
                base_url, headers, tenant_id, client=client
            )
            if err:
                return err
            detail, err = await _trpc(
                base_url, headers, "supportingAssets.getEvidenceDetail",
                "get evidence detail", input={"evidenceId": evidence_id},
                tenant_id=tenant, client=client,
            )
            if err:
                return err
            if not detail:
                return f"Evidence {evidence_id} not found."
            refusal = _evidence_reset_refusal(evidence_id, detail)
            if refusal:
                return refusal
            _, err = await _trpc(
                base_url, headers, "supportingAssets.resetEvidenceState",
                "reset evidence state", input={"evidenceId": evidence_id},
                mutation=True, tenant_id=tenant, client=client,
            )
        if err:
            return err
        return (
            f"Handed evidence {evidence_id} back to its connector; the state "
            "set by a person stays until the connector's next pass."
        )

    async def get_opencrq_evidence(
        evidence_id: str = "", tenant_id: str = ""
    ) -> str:
        if not evidence_id:
            return "evidence_id is required."
        tenant, err = await _resolve_tenant(base_url, headers, tenant_id)
        if err:
            return err
        detail, err = await _trpc(
            base_url, headers, "supportingAssets.getEvidenceDetail",
            "get evidence detail",
            input={"evidenceId": evidence_id}, tenant_id=tenant,
        )
        if err:
            return err
        if not detail:
            return f"Evidence {evidence_id} not found."
        lines = [f"[AssetEvidence] id: {evidence_id}"]
        if detail.get("cveId"):
            lines.append(f"  CVE: {_one_line(detail['cveId'], 40)}")
        if detail.get("mitreTechniqueExternalId"):
            lines.append(
                f"  Technique: {_one_line(detail['mitreTechniqueExternalId'], 20)} "
                f"{_one_line(detail.get('mitreTechniqueName'), 120)}".rstrip()
                + (
                    f" ({_one_line(detail['expectationType'], 40)} test)"
                    if detail.get("expectationType")
                    else ""
                )
            )
        asset = _one_line(detail.get("assetName"), 200) or _one_line(detail.get("assetDerivedName"), 200)
        if asset or detail.get("supportingAssetId"):
            lines.append(
                f"  Asset: {asset or 'unnamed'} "
                f"(id: {detail.get('supportingAssetId')})"
            )
        if detail.get("cveDescription"):
            lines.append(f"  CVE description: {_one_line(detail['cveDescription'], 300)}")
        if detail.get("currentState"):
            lines.append(f"  Current state: {_one_line(detail['currentState'], 40)}")
        history = detail.get("history") or []
        if history:
            lines.append(f"  History ({len(history)}):")
            for h in history[:15]:
                why = "/".join(
                    _one_line(x, 40) for x in (h.get("dismissalType"), h.get("dismissalReasonCode")) if x
                )
                lines.append(
                    f"    - {_one_line(h.get('stateChangedAt'), 40, 'undated')}: {_one_line(h.get('state'), 40, 'unknown')}"
                    + (f" [{why}]" if why else "")
                    + (f" ({_one_line(h['reason'], 300)})" if h.get("reason") else "")
                    + (f" by {_one_line(h['changedByUserName'], 120)}" if h.get("changedByUserName") else "")
                    + (f" [origin: {_one_line(h['origin'], 40)}]" if h.get("origin") else "")
                )
        sources = detail.get("sources") or []
        if sources:
            lines.append(f"  Sources ({len(sources)}):")
            for s in sources:
                lines.append(
                    f"    - {_one_line(s.get('source'), 60, 'unknown source')}, "
                    + (
                        f"connector {s['connectorId']}"
                        if s.get("connectorId")
                        else "connector deleted"
                    )
                    + f", last seen {s.get('lastSeenAt') or 'never'}"
                )
        else:
            lines.append("  Sources: none (attached by hand)")
        return _quarantined("", "\n".join(lines), "evidence (connector findings and user notes)")

    async def list_opencrq_cve_evidences(
        cve_id: str = "", page: int = 1, limit: int = 25, tenant_id: str = ""
    ) -> str:
        cve_id = (cve_id or "").strip()
        if not cve_id:
            return "cve_id is required (e.g. 'CVE-2024-3400')."
        if page < 1:
            return "page must be >= 1."
        if not 1 <= limit <= 500:
            return "limit must be between 1 and 500."
        tenant, err = await _resolve_tenant(base_url, headers, tenant_id)
        if err:
            return err
        result, err = await _trpc(
            base_url, headers, "supportingAssets.getEvidencesForCve",
            "list CVE evidences",
            input={"cveId": cve_id, "page": page, "limit": limit, "sortDesc": False},
            tenant_id=tenant,
        )
        if err:
            return err
        evidences = (result or {}).get("items") or []
        total = (result or {}).get("total", len(evidences))
        if not evidences:
            return _fmt_empty_page(f"No assets have evidence for {cve_id}.", page, total)
        lines = []
        for e in evidences:
            lines.append(
                f"- {_one_line(e.get('assetName'), 200) or _one_line(e.get('assetDerivedName'), 200, 'unnamed')} "
                f"({_one_line(e.get('assetCategoryId'), 60)}, criticality "
                f"{_one_line(e.get('assetCriticality'), 20)}): "
                f"{_one_line(e.get('currentState'), 40, 'no state')}"
                + (
                    f" - {_one_line(e['stateReason'], 100)}"
                    if e.get("stateReason")
                    else ""
                )
                + f" (evidence id: {e.get('evidenceId')})"
            )
        return _quarantined(
            f"Assets affected by {_one_line(cve_id, 40)} (page {page}, showing {len(evidences)} of {total}):",
            "\n".join(lines),
            "CVE evidences (asset names and user notes)",
        )

    async def list_opencrq_unique_findings(
        page: int = 1,
        limit: int = 20,
        source: str = "",
        sort_by: str = "score",
        sort_desc: bool = True,
        tenant_id: str = "",
    ) -> str:
        if page < 1:
            return "page must be >= 1."
        if not 1 <= limit <= 500:
            return "limit must be between 1 and 500."
        source = (source or "").strip().lower()
        if source and source not in _FINDING_SOURCES:
            return "source must be one of: " + ", ".join(_FINDING_SOURCES) + "."
        if sort_by not in _UNIQUE_FINDING_SORTS:
            return "sort_by must be one of: " + ", ".join(_UNIQUE_FINDING_SORTS) + "."
        tenant, err = await _resolve_tenant(base_url, headers, tenant_id)
        if err:
            return err
        payload: dict = {
            "page": page, "limit": limit, "sortBy": sort_by, "sortDesc": bool(sort_desc),
        }
        if source:
            payload["source"] = source
        result, err = await _trpc(
            base_url, headers, "findingScore.listUniqueFindings",
            "list unique findings", input=payload, tenant_id=tenant,
        )
        if err:
            return err
        rows = (result or {}).get("items") or []
        total = (result or {}).get("total", len(rows))
        if not rows:
            return _fmt_empty_page("No findings recorded in this tenant.", page, total)
        lines = []
        for r in rows:
            if r.get("cveId"):
                label = _one_line(r["cveId"], 40) + (f" {_one_line(r['cveTitle'], 200)}" if r.get("cveTitle") else "")
            else:
                label = (
                    f"{_one_line(r.get('mitreTechniqueExternalId'), 20)} "
                    f"{_one_line(r.get('mitreTechniqueName'), 120)}".rstrip()
                    + (f" ({_one_line(r['expectationType'], 40)} test)" if r.get("expectationType") else "")
                )
            details = [
                f"{_one_line(r['band'], 20)} {r['score']}"
                if r.get("score") is not None
                else "not scored yet",
                f"{r.get('affectedAssetCount', 0)} asset(s)",
            ]
            if r.get("knownExploited"):
                details.append("CISA KEV")
            if r.get("epss") is not None:
                details.append(f"EPSS {r['epss']}")
            if r.get("sources"):
                details.append("sources " + ", ".join(_one_line(s, 40) for s in r["sources"]))
            if r.get("firstSeenAt"):
                details.append(f"first seen {str(r['firstSeenAt'])[:10]}")
            lines.append(f"- {label} ({', '.join(details)})")
        return _quarantined(
            f"Findings (page {page}, showing {len(rows)} of {total}):",
            "\n".join(lines),
            "findings (CVE feeds and connector results)",
        )

    async def list_opencrq_cves(
        page: int = 1, limit: int = 20, tenant_id: str = ""
    ) -> str:
        if page < 1:
            return "page must be >= 1."
        if not 1 <= limit <= 500:
            return "limit must be between 1 and 500."
        tenant, err = await _resolve_tenant(base_url, headers, tenant_id)
        if err:
            return err
        result, err = await _trpc(
            base_url, headers, "cve.list", "list CVEs",
            input={"page": page, "limit": limit}, tenant_id=tenant,
        )
        if err:
            return err
        items = (result or {}).get("items") or []
        total = (result or {}).get("total", len(items))
        if not items:
            return _fmt_empty_page("No CVEs in the knowledge base.", page, total)
        return _quarantined(
            f"CVEs (page {page}, showing {len(items)} of {total}):",
            "\n\n".join(_fmt_cve(c, base_url, tenant) for c in items),
            "CVE knowledge base (NIST, CISA KEV and EPSS feeds)",
        )

    async def get_opencrq_cve(cve_id: str = "", tenant_id: str = "") -> str:
        cve_id = (cve_id or "").strip()
        if not cve_id:
            return "cve_id is required (e.g. 'CVE-2024-3400')."
        tenant, err = await _resolve_tenant(base_url, headers, tenant_id)
        if err:
            return err
        cve, err = await _trpc(
            base_url, headers, "cve.getInfo", "get CVE",
            input={"cveId": cve_id}, tenant_id=tenant,
        )
        if err:
            return err
        if not cve:
            return f"{cve_id} not found in the OpenCRQ CVE knowledge base."
        return _untrusted(_fmt_cve(cve, base_url, tenant), "CVE knowledge base (NIST, CISA KEV and EPSS feeds)")

    executor.register("list_opencrq_primary_assets", list_opencrq_primary_assets)
    executor.register(
        "list_opencrq_business_value_categories", list_opencrq_business_value_categories
    )
    executor.register("create_opencrq_primary_asset", create_opencrq_primary_asset)
    executor.register("update_opencrq_primary_asset", update_opencrq_primary_asset)
    executor.register("delete_opencrq_primary_assets", delete_opencrq_primary_assets)
    executor.register(
        "list_opencrq_supporting_assets", list_opencrq_supporting_assets
    )
    executor.register("get_opencrq_supporting_asset", get_opencrq_supporting_asset)
    executor.register("get_opencrq_primary_asset", get_opencrq_primary_asset)
    executor.register(
        "create_opencrq_supporting_asset", create_opencrq_supporting_asset
    )
    executor.register(
        "update_opencrq_supporting_asset", update_opencrq_supporting_asset
    )
    executor.register(
        "reset_opencrq_supporting_asset_to_connectors",
        reset_opencrq_supporting_asset_to_connectors,
    )
    executor.register(
        "delete_opencrq_supporting_assets", delete_opencrq_supporting_assets
    )
    executor.register(
        "list_opencrq_asset_dependencies", list_opencrq_asset_dependencies
    )
    executor.register(
        "create_opencrq_asset_dependency", create_opencrq_asset_dependency
    )
    executor.register(
        "update_opencrq_asset_dependency_roles", update_opencrq_asset_dependency_roles
    )
    executor.register(
        "delete_opencrq_asset_dependencies", delete_opencrq_asset_dependencies
    )
    executor.register("create_opencrq_asset_evidence", create_opencrq_asset_evidence)
    executor.register("update_opencrq_evidence_state", update_opencrq_evidence_state)
    executor.register("delete_opencrq_asset_evidence", delete_opencrq_asset_evidence)
    executor.register("reset_opencrq_evidence_state", reset_opencrq_evidence_state)
    executor.register("get_opencrq_evidence", get_opencrq_evidence)
    executor.register("list_opencrq_cve_evidences", list_opencrq_cve_evidences)
    executor.register("list_opencrq_unique_findings", list_opencrq_unique_findings)
    executor.register("list_opencrq_cves", list_opencrq_cves)
    executor.register("get_opencrq_cve", get_opencrq_cve)
    executor.register("search_opencrq_cves", search_opencrq_cves)

    _tenant_prop = TENANT_PROP

    def _native_props(nullable: bool) -> dict[str, Any]:
        """The schema of a primary asset's native fields; on an update each also takes null (clears it)."""
        clears = " null or an empty string clears it; omit it to keep it." if nullable else ""

        def kind(json_type: str) -> object:
            return [json_type, "null"] if nullable else json_type

        level = {
            "type": kind("string"),
            "enum": [*SECURITY_NEED_LEVELS, *([None] if nullable else [])],
        }
        minutes = {"type": kind("integer"), "minimum": 0, "maximum": _DURATION_MAX_MINUTES}
        return {
            "reference": {
                "type": kind("string"),
                "maxLength": _REFERENCE_MAX_LENGTH,
                "description": f"The asset's own reference code, unique in its organization (max 64 characters).{clears}",
            },
            "business_function": {
                "type": kind("string"),
                "maxLength": _BUSINESS_FUNCTION_MAX_LENGTH,
                "description": f"The business function the asset serves (max 128 characters).{clears}",
            },
            **{
                criterion: {
                    **level,
                    "description": (
                        f"{label} security need, on the scale low < medium < high < very_high. "
                        "The overall level is not set: it is the highest of the four criteria."
                        f"{clears}"
                    ),
                }
                for criterion, label in _SECURITY_NEED_CRITERIA
            },
            **{
                argument: {
                    **minutes,
                    "description": (
                        f"{label} in whole minutes, 0 to {_DURATION_MAX_MINUTES} (10 years); "
                        f"e.g. 240 = 4 hours, 1440 = 1 day.{clears}"
                    ),
                }
                for argument, label in (
                    ("rto_minutes", "Recovery time objective (RTO)"),
                    ("rpo_minutes", "Recovery point objective (RPO)"),
                    ("mtpd_minutes", "Maximum tolerable period of disruption (MTPD)"),
                )
            },
        }

    return [
        ToolDefinition(
            name="list_opencrq_primary_assets",
            description=(
                "List the primary (business) assets of an OpenCRQ "
                "organization - the business processes and information "
                "targeted by risk scenarios - with their business value "
                "categories, reference, security needs (availability/integrity/"
                "confidentiality/traceability and the computed overall level), "
                "business function and RTO/RPO/MTPD. get_opencrq_primary_asset "
                "reads one asset whole."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "organization_id": {
                        "type": "string",
                        "description": "Organization UUID.",
                    },
                    **_tenant_prop,
                },
                "required": ["organization_id"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="list_opencrq_business_value_categories",
            description=(
                "List the OpenCRQ business value categories (the closed "
                "catalogue of what a primary asset is worth to the business, "
                "e.g. personal data, payment card data). A primary asset is "
                "classified into one or more of them."
            ),
            input_schema={"type": "object", "properties": {**_tenant_prop}},
            source=_SOURCE,
        ),
        ToolDefinition(
            name="list_opencrq_supporting_assets",
            description=(
                "List the supporting (technical) assets of an OpenCRQ "
                "tenant - the systems and services that back the primary "
                "assets - page by page, sorted by name. Supports a server-"
                "side text search."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "search": {"type": "string", "description": "Text filter."},
                    "page": {"type": "integer", "minimum": 1, "default": 1},
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 500,
                        "default": 25,
                    },
                    **_tenant_prop,
                },
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="get_opencrq_primary_asset",
            description=(
                "Get every field of one OpenCRQ primary (business) asset: "
                "organization, owner, reference, business function, description, "
                "business value categories, the four security needs (availability, "
                "integrity, confidentiality, traceability; scale low < medium < high "
                "< very_high) with the overall level computed as the highest of them, "
                "and the RTO, RPO and MTPD in minutes."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "primary_asset_id": {
                        "type": "string",
                        "description": "Primary asset UUID (list_opencrq_primary_assets).",
                    },
                    **_tenant_prop,
                },
                "required": ["primary_asset_id"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="get_opencrq_supporting_asset",
            description=(
                "Get an OpenCRQ supporting asset's detail including its "
                "number of vulnerability findings."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "supporting_asset_id": {
                        "type": "string",
                        "description": "Supporting asset UUID.",
                    },
                    **_tenant_prop,
                },
                "required": ["supporting_asset_id"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="search_opencrq_cves",
            description=(
                "Search the OpenCRQ CVE knowledge base (synced from NIST "
                "NVD, FIRST EPSS, and CISA KEV) by CVE ID or keyword."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "CVE ID or keyword (min 3 chars).",
                    },
                    **_tenant_prop,
                },
                "required": ["query"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="create_opencrq_primary_asset",
            description=(
                "Create a primary (business) asset in an OpenCRQ "
                "organization, classified into one or more business value "
                "categories (list_opencrq_business_value_categories), with "
                "optional reference, business function, security needs "
                "(low < medium < high < very_high) and RTO/RPO/MTPD in minutes. "
                "Confirm with the user before creating."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "organization_id": {
                        "type": "string",
                        "description": "Organization UUID.",
                    },
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "owner_user_id": {
                        "type": "string",
                        "description": "Owner user UUID.",
                    },
                    "business_value_category_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "description": "Business value category UUIDs.",
                    },
                    **_native_props(nullable=False),
                    **_tenant_prop,
                },
                "required": [
                    "organization_id",
                    "name",
                    "business_value_category_ids",
                ],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="update_opencrq_primary_asset",
            description=(
                "Update a primary (business) asset in OpenCRQ (name, "
                "description, owner, business value categories, reference, "
                "business function, security needs on the scale low < medium < "
                "high < very_high, RTO/RPO/MTPD in minutes). Fields you omit are "
                "left unchanged; null clears a reference, business function, "
                "security need or duration. Confirm with the user before "
                "updating."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "primary_asset_id": {
                        "type": "string",
                        "description": "Primary asset UUID.",
                    },
                    "name": {"type": "string"},
                    "description": {
                        "type": "string",
                        "description": "An empty string removes the description.",
                    },
                    "owner_user_id": {
                        "type": "string",
                        "description": (
                            "UUID of the user who owns the asset "
                            "(list_opencrq_tenant_users); an empty string removes the owner."
                        ),
                    },
                    "business_value_category_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "description": "Replaces the asset's categories.",
                    },
                    **_native_props(nullable=True),
                    **_tenant_prop,
                },
                "required": ["primary_asset_id"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="delete_opencrq_primary_assets",
            description=(
                "Delete one or more OpenCRQ primary assets (bulk). This is "
                "irreversible and removes their dependency links - "
                "explicitly confirm the exact assets with the user before "
                "deleting."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "primary_asset_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Primary asset UUIDs to delete.",
                    },
                    **_tenant_prop,
                },
                "required": ["primary_asset_ids"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="create_opencrq_supporting_asset",
            description=(
                "Create a supporting (technical) asset in OpenCRQ with a "
                "category, exposure, and optional criticality, subcategory, "
                "url, linked person, owner name, groups, hand-set tags and a "
                "property block (hostname, IPs, ... per family). organization_id maps the "
                "asset into that risk scope when its estate is closed (an "
                "open estate ignores it). " + _CONNECTOR_NOTE + " Confirm "
                "with the user before creating."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "category": {
                        "type": "string",
                        "enum": ASSET_CATEGORIES,
                    },
                    "exposure": {
                        "type": "string",
                        "enum": ASSET_EXPOSURES,
                        "description": "Defaults to unknown.",
                    },
                    "description": {"type": "string"},
                    "owner_user_id": {"type": "string"},
                    "criticality": {
                        "type": "string",
                        "enum": ASSET_CRITICALITIES,
                    },
                    "subcategory": {
                        "type": "string",
                        "enum": _ASSET_SUBCATEGORY_VALUES,
                        "description": (
                            "Must belong to the category (host: server, "
                            "workstation, ...; generic_asset has none)."
                        ),
                    },
                    "url": {"type": "string"},
                    "linked_person": {"type": "string"},
                    "tag_names": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Tags to set by hand on the asset.",
                    },
                    "owner_name": {"type": "string", "description": _OWNER_NAME_DESCRIPTION},
                    "groups": _GROUPS_SCHEMA,
                    "properties": _ASSET_PROPERTIES_SCHEMA,
                    "organization_id": {
                        "type": "string",
                        "description": (
                            "Risk scope (organization) UUID to map the asset "
                            "into."
                        ),
                    },
                    **_tenant_prop,
                },
                "required": ["name", "category"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="update_opencrq_supporting_asset",
            description=(
                "Update a supporting (technical) asset in OpenCRQ (name, "
                "category, exposure, criticality, description, owner, "
                "subcategory, url, linked person, tags, groups, property block). "
                "Fields you omit are left unchanged; an empty string removes "
                "the description, owner, subcategory, url or linked person. "
                "Changing the category drops a subcategory that no longer fits, "
                "unless the same call sets one; a property block typed by hand "
                "for another family stops applying until the asset is moved "
                "back to it, unless the same call sends a new block. "
                + _CONNECTOR_NOTE + " Confirm with the user "
                "before updating."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "supporting_asset_id": {
                        "type": "string",
                        "description": "Supporting asset UUID.",
                    },
                    "name": {"type": "string"},
                    "category": {
                        "type": "string",
                        "enum": ASSET_CATEGORIES,
                    },
                    "exposure": {
                        "type": "string",
                        "enum": ASSET_EXPOSURES,
                    },
                    "description": {
                        "type": "string",
                        "description": "An empty string removes the description.",
                    },
                    "owner_user_id": {
                        "type": "string",
                        "description": (
                            "UUID of the user who owns the asset "
                            "(list_opencrq_tenant_users); an empty string removes the owner."
                        ),
                    },
                    "criticality": {
                        "type": "string",
                        "enum": ASSET_CRITICALITIES,
                    },
                    "subcategory": {
                        "type": "string",
                        "enum": [*_ASSET_SUBCATEGORY_VALUES, ""],
                        "description": (
                            "Must belong to the category, given or current. "
                            "An empty string removes it."
                        ),
                    },
                    "url": {
                        "type": "string",
                        "description": "An empty string removes the url.",
                    },
                    "linked_person": {
                        "type": "string",
                        "description": "An empty string removes the linked person.",
                    },
                    "tag_names": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": _TAG_NAMES_DESCRIPTION,
                    },
                    "owner_name": {"type": "string", "description": _OWNER_NAME_DESCRIPTION},
                    "groups": _GROUPS_SCHEMA,
                    "properties": _ASSET_PROPERTIES_SCHEMA,
                    **_tenant_prop,
                },
                "required": ["supporting_asset_id"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="reset_opencrq_supporting_asset_to_connectors",
            description=(
                "Hand an OpenCRQ supporting asset back to its connectors: "
                "every value set by hand on it (name, category, subcategory, "
                "exposure, criticality, description, url, linked person and "
                "the property block typed by hand) is erased and re-derived "
                "from what its connectors report, and the next connector "
                "pass is free to update it again. This cannot be undone. "
                "Tags and the owner are kept. Works only while a live "
                "connector still observes the asset: for an asset created "
                "by hand, or whose connector was deleted, the tool changes "
                "nothing and says so. Queues a finding-score recompute. "
                "Confirm with the user before resetting."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "supporting_asset_id": {
                        "type": "string",
                        "description": "Supporting asset UUID.",
                    },
                    **_tenant_prop,
                },
                "required": ["supporting_asset_id"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="delete_opencrq_supporting_assets",
            description=(
                "Delete one or more OpenCRQ supporting assets (bulk). This "
                "is irreversible and removes their evidences and dependency "
                "links - explicitly confirm the exact assets with the user "
                "before deleting."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "supporting_asset_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Supporting asset UUIDs to delete.",
                    },
                    **_tenant_prop,
                },
                "required": ["supporting_asset_ids"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="list_opencrq_asset_dependencies",
            description=(
                "List the dependency links of an OpenCRQ asset: "
                "support-to-primary links (which business assets a "
                "technical asset supports, and with which roles) and "
                "support-to-support links (technical dependency chains)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "supporting_asset_id": {
                        "type": "string",
                        "description": "Supporting asset UUID.",
                    },
                    "primary_asset_id": {
                        "type": "string",
                        "description": "Primary asset UUID.",
                    },
                    **_tenant_prop,
                },
                # At least one asset selector so pre-dispatch jsonschema
                # validation rejects an empty call before the tool
                # executes (the runtime check stays as defense in depth).
                "anyOf": [
                    {"required": ["supporting_asset_id"]},
                    {"required": ["primary_asset_id"]},
                ],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="create_opencrq_asset_dependency",
            description=(
                "Create a dependency link between OpenCRQ assets: "
                "support_to_primary (technical asset supports a business "
                "asset, with the roles it plays for it) or support_to_support "
                "(provider technical asset -> dependent technical asset). "
                "Confirm with the user before creating."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "link_type": {
                        "type": "string",
                        "enum": _LINK_TYPES,
                    },
                    "supporting_asset_id": {
                        "type": "string",
                        "description": "For support_to_primary links.",
                    },
                    "primary_asset_id": {
                        "type": "string",
                        "description": "For support_to_primary links.",
                    },
                    "roles": {
                        "type": "array",
                        "items": {"type": "string", "enum": _LINK_ROLES},
                        "description": (
                            "For support_to_primary links: what the "
                            "supporting asset does for the business asset. "
                            "Omitted, the link is unknown, which inherits "
                            "every security criterion in full; unknown "
                            "cannot be combined with another role."
                        ),
                    },
                    "provider_asset_id": {
                        "type": "string",
                        "description": "For support_to_support links.",
                    },
                    "dependent_asset_id": {
                        "type": "string",
                        "description": "For support_to_support links.",
                    },
                    **_tenant_prop,
                },
                "required": ["link_type"],
                # Encode the per-link_type ID requirements so pre-dispatch
                # jsonschema validation rejects incomplete calls before the
                # tool executes (the runtime checks stay as defense in
                # depth for callers that bypass schema validation).
                "allOf": [
                    {
                        "if": {
                            "properties": {
                                "link_type": {"const": "support_to_primary"},
                            },
                            "required": ["link_type"],
                        },
                        "then": {
                            "required": [
                                "supporting_asset_id",
                                "primary_asset_id",
                            ],
                        },
                    },
                    {
                        "if": {
                            "properties": {
                                "link_type": {"const": "support_to_support"},
                            },
                            "required": ["link_type"],
                        },
                        "then": {
                            "required": [
                                "provider_asset_id",
                                "dependent_asset_id",
                            ],
                        },
                    },
                ],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="update_opencrq_asset_dependency_roles",
            description=(
                "Replace the roles of existing support-to-primary dependency "
                "links of OpenCRQ assets; the same roles apply to every "
                "listed link, and support-to-support links have none. "
                "list_opencrq_asset_dependencies shows each link's id and "
                "its current roles. The answer says how many links were "
                "updated and lists the skipped ones (gone: deleted or not "
                "visible since they were read). "
                "Confirm with the user before updating."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "link_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "maxItems": _MAX_LINKS_PER_WRITE,
                        "description": (
                            "Support-to-primary link UUIDs to update "
                            f"(at most {_MAX_LINKS_PER_WRITE}, duplicates "
                            "dropped)."
                        ),
                    },
                    "roles": {
                        "type": "array",
                        "items": {"type": "string", "enum": _LINK_ROLES},
                        "minItems": 1,
                        "description": (
                            "What the supporting asset does for the business "
                            "asset; replaces the links' current roles. At "
                            "least one; unknown cannot be combined with "
                            "another role."
                        ),
                    },
                    **_tenant_prop,
                },
                "required": ["link_ids", "roles"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="delete_opencrq_asset_dependencies",
            description=(
                "Delete one or more OpenCRQ asset dependency links (bulk). "
                "This is irreversible - explicitly confirm the exact links "
                "with the user before deleting."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "link_type": {
                        "type": "string",
                        "enum": _LINK_TYPES,
                    },
                    "link_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Dependency link UUIDs to delete.",
                    },
                    **_tenant_prop,
                },
                "required": ["link_type", "link_ids"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="create_opencrq_asset_evidence",
            description=(
                "Attach a CVE as vulnerability evidence to an OpenCRQ "
                "supporting asset. cve_record_id is the CVE row UUID from "
                "the knowledge base (search_opencrq_cves returns it). "
                "Needs a user session (a service account is refused). "
                "Confirm with the user before creating."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "supporting_asset_id": {
                        "type": "string",
                        "description": "Supporting asset UUID.",
                    },
                    "cve_record_id": {
                        "type": "string",
                        "description": "CVE knowledge base row UUID.",
                    },
                    **_tenant_prop,
                },
                "required": ["supporting_asset_id", "cve_record_id"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="update_opencrq_evidence_state",
            description=(
                "Set the state of an OpenCRQ asset evidence: applicable or "
                "dismissed (a dismissal requires a type, a reason code of "
                "that type and a written reason). Needs a user session (a "
                "service account is refused). Confirm with the user before "
                "updating."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "evidence_id": {
                        "type": "string",
                        "description": "Evidence UUID.",
                    },
                    "state": {
                        "type": "string",
                        "enum": ["applicable", "dismissed"],
                    },
                    "dismissal_type": {
                        "type": "string",
                        "enum": list(_DISMISSAL_REASON_CODES),
                        "description": (
                            "Required when state=dismissed: risk_accepted "
                            "(a real finding the organization lives with) or "
                            "false_positive (not true here)."
                        ),
                    },
                    "reason_code": {
                        "type": "string",
                        "enum": sorted(
                            {c for cs in _DISMISSAL_REASON_CODES.values() for c in cs}
                        ),
                        "description": (
                            "Required when state=dismissed. risk_accepted: "
                            + ", ".join(_DISMISSAL_REASON_CODES["risk_accepted"])
                            + ". false_positive: "
                            + ", ".join(_DISMISSAL_REASON_CODES["false_positive"])
                            + "."
                        ),
                    },
                    "reason": {
                        "type": "string",
                        "maxLength": _REASON_MAX,
                        "description": "Required when state=dismissed.",
                    },
                    **_tenant_prop,
                },
                "required": ["evidence_id", "state"],
                # Encode "dismissed requires its type, code and reason" so
                # pre-dispatch jsonschema validation rejects the call before
                # the tool executes (the runtime check stays as defense in
                # depth).
                "allOf": [
                    {
                        "if": {
                            "properties": {"state": {"const": "dismissed"}},
                            "required": ["state"],
                        },
                        "then": {
                            "required": ["dismissal_type", "reason_code", "reason"],
                        },
                    },
                ],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="delete_opencrq_asset_evidence",
            description=(
                "Delete an OpenCRQ asset evidence that a person attached by "
                "hand (create_opencrq_asset_evidence), with its state history "
                "and score. This is irreversible. An evidence a connector "
                "reported is refused, whatever the connector's fate: "
                "get_opencrq_evidence lists its sources, and none means it "
                "was attached by hand; set a connector's evidence aside with "
                "update_opencrq_evidence_state instead. evidence_id comes "
                "from create_opencrq_asset_evidence's answer, "
                "list_opencrq_cve_evidences or get_opencrq_evidence. Needs a "
                "user session (a service account is refused). Confirm with "
                "the user before deleting."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "evidence_id": {
                        "type": "string",
                        "description": "Evidence UUID.",
                    },
                    **_tenant_prop,
                },
                "required": ["evidence_id"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="reset_opencrq_evidence_state",
            description=(
                "Hand an OpenCRQ asset evidence back to its connector, which "
                "may override the state a person set on its next pass (a "
                "dismissal can come back as applicable). The current state "
                "and the history are kept. Works only when a live connector "
                "still observes the evidence and its latest state was set by "
                "a person; otherwise the tool refuses without changing "
                "anything (OpenCRQ itself would answer an internal error). "
                "get_opencrq_evidence shows both: a source with a connector "
                "id, and a latest history entry with origin user. "
                "evidence_id comes from create_opencrq_asset_evidence's "
                "answer, list_opencrq_cve_evidences or get_opencrq_evidence. "
                "Needs a user session (a service account is refused). "
                "Confirm with the user before resetting."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "evidence_id": {
                        "type": "string",
                        "description": "Evidence UUID.",
                    },
                    **_tenant_prop,
                },
                "required": ["evidence_id"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="get_opencrq_evidence",
            description=(
                "Get an OpenCRQ asset evidence's detail: the CVE or technique, "
                "the asset, its current state, its state change history with "
                "who or what set each state (origin), and the connectors "
                "that report it."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "evidence_id": {
                        "type": "string",
                        "description": "Evidence UUID.",
                    },
                    **_tenant_prop,
                },
                "required": ["evidence_id"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="list_opencrq_cve_evidences",
            description=(
                "List the OpenCRQ supporting assets that have evidence for "
                "a given CVE, with their applicability state (which assets "
                "are affected by this vulnerability), page by page."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "cve_id": {
                        "type": "string",
                        "description": "CVE identifier (e.g. CVE-2024-3400).",
                    },
                    "page": {"type": "integer", "minimum": 1, "default": 1},
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 500,
                        "default": 25,
                    },
                    **_tenant_prop,
                },
                "required": ["cve_id"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="list_opencrq_unique_findings",
            description=(
                "List the tenant's vulnerability findings in OpenCRQ, one "
                "row per CVE or per tested technique, with severity band and "
                "score, number of affected assets, CISA KEV, EPSS and "
                "reporting sources, page by page. Needs a user session (a "
                "service account is refused)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "page": {"type": "integer", "minimum": 1, "default": 1},
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 500,
                        "default": 20,
                    },
                    "source": {
                        "type": "string",
                        "enum": _FINDING_SOURCES,
                        "description": (
                            "Only the findings this source reports (user: "
                            "no reporting connector)."
                        ),
                    },
                    "sort_by": {
                        "type": "string",
                        "enum": _UNIQUE_FINDING_SORTS,
                        "default": "score",
                    },
                    "sort_desc": {"type": "boolean", "default": True},
                    **_tenant_prop,
                },
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="list_opencrq_cves",
            description=(
                "Page through the OpenCRQ CVE knowledge base (synced from "
                "NIST NVD, FIRST EPSS, and CISA KEV)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "page": {"type": "integer", "minimum": 1, "default": 1},
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 500,
                        "default": 20,
                    },
                    **_tenant_prop,
                },
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="get_opencrq_cve",
            description=(
                "Get a CVE's detail from the OpenCRQ knowledge base (CVSS, "
                "EPSS, CISA KEV, description) by CVE identifier."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "cve_id": {
                        "type": "string",
                        "description": "CVE identifier (e.g. CVE-2024-3400).",
                    },
                    **_tenant_prop,
                },
                "required": ["cve_id"],
            },
            source=_SOURCE,
        ),
    ]
