# SPDX-License-Identifier: Apache-2.0

"""CTEM metrics collection - per-user, live, scoped to what the user can see.

XTM One never stores an OpenCTI/OpenAEV service token. Registration creates a
single company-managed ``platform_managed`` integration that holds only the
platform ``base_url``; every outbound call is authenticated with a short-lived
per-user Ed25519 JWT minted at request time (``_get_token`` ->
``_generate_platform_jwt`` -> ``issue_xtm_jwt``). OpenCTI and OpenAEV verify that
JWT against XTM One's ``/xtm/auth/jwks`` and resolve the **acting user by email**,
so every query returns exactly what *that user* is allowed to see.

Therefore CTEM metrics are gathered **live, per user** (the requesting user),
not by a global scheduled job. This module exposes ``build_posture_for_user``,
which the ``/api/v1/ctem-metrics`` endpoints call (with a short Redis cache).

Collection is **vertical-aware**: ``build_posture_for_user(db, user_id, vertical)``
dispatches to the cyber / FIMI / fraud collectors. The five CTEM stages are
universal; each vertical emits its own per-stage facts (see ``ctem_verticals``).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import case, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import release_db_connection_before_loop
from app.core.utils import format_exception_message
from app.integrations.providers.openaev._common import _api, _search_body
from app.integrations.providers.opencti._common import _graphql
from app.integrations.providers.opencti.capabilities import attach_probe
from app.models.integration import Integration
from app.services.ctem_funnel import EPSS_THRESHOLD as FUNNEL_EPSS_THRESHOLD
from app.services.ctem_funnel import build_funnel
from app.services.ctem_scoring import pct
from app.services.ctem_verticals import (
    VERTICAL_CYBER,
    VERTICAL_FIMI,
    VERTICAL_FRAUD,
    normalize_vertical,
)
from app.services.integration_tools import _get_token

logger = logging.getLogger(__name__)

_SIM_SAMPLE = 100
_ATTACK_SCAN_SIMS = 20
# Expectations-drift is one request per simulation, so only the most recent
# finished simulations of the window are probed. Drift is a slow-moving
# content-hygiene signal - a sample is enough to tell "clean" from "stale".
_AEV_DRIFT_SAMPLE = 10
# Validation donuts mirror the OpenAEV home dashboard, which aggregates
# expectation results over a selectable time window. Default to ~3 months so the
# Prevention / Detection / Vulnerability / Human-response success rates and
# donuts line up with what the home shows for its default range.
_AEV_VALIDATION_WINDOW_DAYS = 90
# Safety bound on threat T-code pagination (500 Attack-Patterns/page): ATT&CK has
# well under 10000 techniques, so 20 pages can never truncate a real dataset while
# still preventing a pathological unbounded loop.
_OCTI_TCODE_PAGE_CAP = 20
_CASE_CLOSED_TOKENS = ("clos", "resolv", "done", "complete", "remediat")

# A live posture build is dominated by ~13 OpenCTI / ~12 OpenAEV round-trips that
# used to run strictly one-after-another (~20-30s on a large instance). They are
# independent, so each collector now fans them out with ``asyncio.gather`` -
# bounded per provider so a build never opens more than this many simultaneous
# upstream connections (the API layer separately caps how many builds run at once,
# so total upstream pressure stays bounded even under a dashboard load burst).
_OCTI_MAX_CONCURRENCY = 6
_AEV_MAX_CONCURRENCY = 6


async def _gather_bounded(limit: int, *coros: Any) -> list[Any]:
    """Run *coros* concurrently with at most *limit* in flight; results in order.

    Lets a collector parallelize its independent upstream queries (the bulk of a
    live build) while capping the simultaneous load on OpenCTI / OpenAEV. Each
    coroutine's own error handling is preserved (the helpers degrade to empty /
    ``None`` on failure), so one slow or failing query never blocks the rest.
    """
    sem = asyncio.Semaphore(limit)

    async def _run(coro: Any) -> Any:
        async with sem:
            return await coro

    return await asyncio.gather(*(_run(c) for c in coros))


def _to_int(value: Any, default: int = 0) -> int:
    """Coerce a possibly-None / non-numeric value to ``int``."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _closure_pct(closed: int, total: int) -> float:
    """Case / takedown closure rate. An empty queue (0 cases) is a fully-closed
    loop, so it scores 100 (nothing open to remediate) rather than null - this
    keeps the Remediation / Mobilization stage scored instead of showing "--".
    Only called when the product is connected, so 0 means a real empty queue."""
    return pct(closed, total) if total else 100.0


async def _user_creds(
    db: AsyncSession, user_id: str, provider: str
) -> tuple[str, dict[str, str]] | None:
    """Resolve ``(base_url, auth_headers)`` for *provider*, scoped to *user_id*.

    Finds the company-managed integration for the provider and asks
    ``integration_tools._get_token`` for a token. For a ``platform_managed``
    integration this mints a per-user Ed25519 JWT (the user's identity), so the
    platform returns data scoped to that user. Returns ``None`` when the product
    is not registered or no token can be minted (e.g. no user context).
    """
    # Deterministic + non-fatal: there is no uniqueness constraint on
    # (provider, is_company_managed), so use order_by + limit(1) / first()
    # rather than scalar_one_or_none(), which would raise MultipleResultsFound
    # (500 on the dashboard) if duplicates ever exist.
    from app.api.v1.helpers import integration_visible_filter

    result = await db.execute(
        select(Integration)
        .where(
            Integration.provider == provider,
            # access_restricted: honoured - a restricted row serves its grantees only.
            Integration.is_company_managed.is_(True),
            await integration_visible_filter(db, user_id),
        )
        # A registered (platform_managed) row is preferred: it mints the
        # user's own JWT. Without one, a company-managed row (a static key on
        # an unregistered deployment) serves the posture as it always did.
        .order_by(
            case((Integration.auth_type == "platform_managed", 0), else_=1),
            Integration.created_at,
        )
        .limit(1)
    )
    integ = result.scalars().first()
    if not integ:
        logger.info("CTEM %s: no company-managed integration registered — skipping", provider)
        return None
    try:
        token = await _get_token(db, str(integ.id), provider, user_id=user_id)
    except Exception as exc:  # never let one provider break the dashboard
        logger.warning("CTEM: token resolution failed for %s: %s", provider, format_exception_message(exc))
        return None
    if not token or "||" not in token:
        logger.info(
            "CTEM %s: could not mint a per-user token for user %s "
            "(integration %s, auth_type=%s) — skipping",
            provider,
            user_id,
            integ.id,
            integ.auth_type,
        )
        return None
    base_url, _, api_token = token.partition("||")
    base_url = base_url.rstrip("/")
    if not base_url or not api_token:
        logger.info(
            "CTEM %s: integration %s is missing a base_url or token — skipping",
            provider,
            integ.id,
        )
        return None
    # Creds are resolved; the caller (a CTEM collector) now runs a long sequence
    # of httpx calls to OpenCTI/OpenAEV (~20-30s) with NO further DB use. Release
    # the pooled DB connection first - mirroring release_db_connection_before_loop
    # for the agentic loop - so a live collection never pins a connection across
    # that external I/O. Pinning it was the root cause of the dashboard pool
    # exhaustion under concurrent loads. SQLAlchemy transparently re-acquires a
    # connection on the session's next use (the post-collection snapshot write).
    await release_db_connection_before_loop(db)
    return base_url, {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json",
    }


# ── OpenCTI (GraphQL) ────────────────────────────────────────────────────────

# Counts use OpenCTI's dedicated count queries (`stixCoreObjectsMultiNumber` +
# `stixCyberObservablesNumber`), NOT `pageInfo.globalCount` on connection queries
# (which is unreliable for pure counts). `total` is the unbounded count.
# `stixCoreObjectsMultiNumber` batches every count into ONE root call, so
# OpenCTI's per-field alias cap (max 2) never applies. Every `types` value MUST
# be a valid Stix Core Object type (entity OR a concrete observable type like
# "Domain-Name") - the resolver silently drops unknown types and counts ALL.


def _fg(filters: list[dict], mode: str = "and") -> dict:
    """Build a FilterGroup input (passed to OpenCTI as a GraphQL variable)."""
    return {"mode": mode, "filters": filters, "filterGroups": []}


# Cyber vulnerability / indicator count refinements (FilterGroup variables).
_F_KEV = _fg([{"key": ["x_opencti_cisa_kev"], "values": ["true"]}])
_F_CRITICAL = _fg([{"key": ["x_opencti_cvss_base_score"], "values": ["9"], "operator": "gte"}])
_F_HIGH_EPSS = _fg([{"key": ["x_opencti_epss_score"], "values": ["0.5"], "operator": "gt"}])
_F_CRIT_OR_KEV = _fg(
    [
        {"key": ["x_opencti_cisa_kev"], "values": ["true"]},
        {"key": ["x_opencti_cvss_base_score"], "values": ["9"], "operator": "gte"},
    ],
    mode="or",
)
# Funnel "exploitable" layer (#11546): known-exploited or EPSS above the threshold.
_F_EXPLOITABLE_FILTERS = [
    {"key": ["x_opencti_cisa_kev"], "values": ["true"]},
    {"key": ["x_opencti_epss_score"], "values": [str(FUNNEL_EPSS_THRESHOLD)], "operator": "gt"},
]
_F_EXPLOITABLE = _fg(_F_EXPLOITABLE_FILTERS, mode="or")
_F_VALID_IND = _fg([{"key": ["revoked"], "values": ["false"]}])
_F_DETECTION_IND = _fg([{"key": ["x_opencti_detection"], "values": ["true"]}])
_F_PHISHING_INFRA = _fg([{"key": ["infrastructure_types"], "values": ["phishing"]}])

# Ordered count specs -> one `stixCoreObjectsMultiNumber` call (key, types, filter).
_CYBER_COUNT_SPECS: list[tuple[str, list[str], dict | None]] = [
    ("reports", ["Report"], None),
    ("vulnerabilities", ["Vulnerability"], None),
    ("indicators", ["Indicator"], None),
    ("intrusion_sets", ["Intrusion-Set"], None),
    ("malwares", ["Malware"], None),
    ("attack_patterns", ["Attack-Pattern"], None),
    ("incidents", ["Incident"], None),
    ("case_incidents", ["Case-Incident"], None),
    ("case_rfis", ["Case-Rfi"], None),
    ("threat_actor_groups", ["Threat-Actor-Group"], None),
    ("campaigns", ["Campaign"], None),
    ("kev", ["Vulnerability"], _F_KEV),
    ("critical", ["Vulnerability"], _F_CRITICAL),
    ("high_epss", ["Vulnerability"], _F_HIGH_EPSS),
    ("critical_or_kev", ["Vulnerability"], _F_CRIT_OR_KEV),
    ("indicators_valid", ["Indicator"], _F_VALID_IND),
    ("indicators_detection", ["Indicator"], _F_DETECTION_IND),
    # Last, so every count above keeps its place in the multi-number answer.
    ("exploitable", ["Vulnerability"], _F_EXPLOITABLE),
]

_FIMI_COUNT_SPECS: list[tuple[str, list[str], dict | None]] = [
    ("narratives", ["Narrative"], None),
    ("channels", ["Channel"], None),
    ("events", ["Event"], None),
    ("languages", ["Language"], None),
    ("media", ["Media-Content"], None),
    ("incidents", ["Incident"], None),
]

_FRAUD_COUNT_SPECS: list[tuple[str, list[str], dict | None]] = [
    ("brands", ["Organization"], None),
    ("domains", ["Domain-Name"], None),
    ("urls", ["Url"], None),
    ("hostnames", ["Hostname"], None),
    ("emails", ["Email-Addr"], None),
    ("credentials", ["Credential"], None),
    ("cards", ["Payment-Card"], None),
    ("wallets", ["Cryptocurrency-Wallet"], None),
    ("user_accounts", ["User-Account"], None),
    ("personas", ["Persona"], None),
    ("indicators", ["Indicator"], None),
    ("indicators_valid", ["Indicator"], _F_VALID_IND),
    ("indicators_detection", ["Indicator"], _F_DETECTION_IND),
    ("infrastructure", ["Infrastructure"], None),
    ("phishing_infra", ["Infrastructure"], _F_PHISHING_INFRA),
    ("malwares", ["Malware"], None),
    ("takedown_cases", ["Case-Rft"], None),
]

# ── Generic OpenCTI query primitives ─────────────────────────────────────────
_OCTI_MULTI = """
query CtemMulti($params: [StixCoreObjectsNumberParameters]) {
  entities: stixCoreObjectsMultiNumber(numberParameters: $params) { total }
}
"""

_OCTI_OBS_TOTAL = """
query CtemObs { observables: stixCyberObservablesNumber { total } }
"""

_OCTI_REL_NUMBER = """
query CtemRel($rel: [String], $from: [String], $to: [String]) {
  stixRelationshipsNumber(relationship_type: $rel, fromTypes: $from, toTypes: $to) { total }
}
"""

_OCTI_ATTR_DIST = """
query CtemAttrDist($types: [String], $field: String!) {
  dist: stixCoreObjectsDistribution(types: $types, field: $field, operation: count, limit: 50) { label value }
}
"""

_OCTI_CASE_STATUS = """
query CtemCaseStatus($types: [String]) {
  caseStatus: stixCoreObjectsDistribution(types: $types, field: "x_opencti_workflow_id", operation: count, limit: 50) {
    label value entity { ... on Status { template { name } } }
  }
}
"""

# Cyber-only queries (ATT&CK coverage, top CVSS vulns). Severity distribution is
# collected via the generic ``_octi_attr_dist`` helper, not a dedicated query.
# Detection coverage counts only techniques pointed at by a still-live indicator
# (``revoked = false``): a revoked indicator no longer provides active detection,
# so its technique must not be credited as covered. Reuses the shared
# ``_F_VALID_IND`` filter (defined above), applied to the source side of the
# ``indicates`` relationship via ``dynamicFrom``.
_OCTI_COVERAGE = """
query CtemCoverage($activeIndicator: FilterGroup) {
  indicates: stixRelationshipsDistribution(relationship_type: ["indicates"], toTypes: ["Attack-Pattern"], field: "internal_id", isTo: true, operation: count, limit: 5000, dynamicFrom: $activeIndicator) { label }
  detects: stixRelationshipsDistribution(relationship_type: ["detects"], toTypes: ["Attack-Pattern"], field: "internal_id", isTo: true, operation: count, limit: 5000) { label }
}
"""

_OCTI_COVERAGE_FALLBACK = """
query CtemCoverageFallback($activeIndicator: FilterGroup) {
  indicates: stixRelationshipsDistribution(relationship_type: ["indicates"], toTypes: ["Attack-Pattern"], field: "internal_id", isTo: true, operation: count, limit: 5000, dynamicFrom: $activeIndicator) { label }
}
"""

# Unfiltered ``indicates`` + ``detects`` for builds that support ``detects`` but
# reject the ``dynamicFrom`` (non-revoked) argument: keeps detection-rule coverage
# (the ``detects`` half) and a best-effort all-indicator ``indicates`` count
# instead of dropping ``detects`` outright.
_OCTI_COVERAGE_UNFILTERED_DETECTS = """
query CtemCoverageUnfilteredDetects {
  indicates: stixRelationshipsDistribution(relationship_type: ["indicates"], toTypes: ["Attack-Pattern"], field: "internal_id", isTo: true, operation: count, limit: 5000) { label }
  detects: stixRelationshipsDistribution(relationship_type: ["detects"], toTypes: ["Attack-Pattern"], field: "internal_id", isTo: true, operation: count, limit: 5000) { label }
}
"""

# Final last-resort coverage query: ``indicates`` only, unfiltered. Used when even
# the unfiltered ``indicates`` + ``detects`` query fails (the build rejects the
# ``detects`` relationship type too), so detection coverage still degrades to a
# best-effort indicator-only value instead of silently zeroing out.
_OCTI_COVERAGE_UNFILTERED = """
query CtemCoverageUnfiltered {
  indicates: stixRelationshipsDistribution(relationship_type: ["indicates"], toTypes: ["Attack-Pattern"], field: "internal_id", isTo: true, operation: count, limit: 5000) { label }
}
"""

# FALLBACK source for the threat-technique T-code set: ATT&CK techniques USED by
# the threats OpenCTI tracks (intrusion sets, malware, campaigns, threat actors),
# keyed by MITRE external id (T-code). This is a ``stixRelationshipsDistribution``
# and is therefore hard-capped at 100 buckets (``MAX_AGGREGATION_SIZE`` in
# engine.ts), so it is used ONLY when the uncapped paginated
# ``_OCTI_THREAT_TECH_CODES`` fetch fails - otherwise the capped set would
# truncate the cross-product threat-informed coverage on threat-rich tenants.
_OCTI_THREAT_TECH = """
query CtemThreatTech {
  threatTech: stixRelationshipsDistribution(relationship_type: ["uses"], fromTypes: ["Intrusion-Set", "Malware", "Campaign", "Threat-Actor-Group"], toTypes: ["Attack-Pattern"], field: "internal_id", isTo: true, operation: count, limit: 1000) {
    label entity { ... on AttackPattern { x_mitre_id } }
  }
}
"""

# Distinct ATT&CK techniques used by tracked threats - an UNCAPPED count via a
# ``regardingOf`` "uses" filter on the Attack-Pattern count. NOT a distribution:
# OpenCTI hard-caps every distribution aggregation at 100 buckets
# (``MAX_AGGREGATION_SIZE`` in engine.ts), which silently saturated the old
# distribution-based count at 100. ``stixCoreObjectsNumber.total`` is unbounded.
_F_THREAT_USES = _fg(
    [{"key": ["regardingOf"], "values": [{"key": "relationship_type", "values": ["uses"]}]}]
)
_OCTI_THREAT_TECH_COUNT = """
query CtemThreatTechCount($filters: FilterGroup) {
  count: stixCoreObjectsNumber(types: ["Attack-Pattern"], filters: $filters) { total }
}
"""

# Full (uncapped) set of ATT&CK technique T-codes used by tracked threats. Pages
# the ``stixCoreObjects`` connection for Attack-Patterns matching the SAME
# ``regardingOf`` "uses" filter as ``_OCTI_THREAT_TECH_COUNT`` and reads each one's
# MITRE ``x_mitre_id``. A connection query (not a distribution) is required so the
# set is NOT truncated at 100 buckets and stays consistent with the uncapped
# ``threat_techniques`` count that drives Discovery's threat-informed coverage.
_OCTI_THREAT_TECH_CODES = """
query CtemThreatTechCodes($filters: FilterGroup, $cursor: ID) {
  stixCoreObjects(types: ["Attack-Pattern"], filters: $filters, first: 500, after: $cursor) {
    pageInfo { endCursor hasNextPage }
    edges { node { ... on AttackPattern { id x_mitre_id killChainPhases { kill_chain_name phase_name x_opencti_order } } } }
  }
}
"""

# The Discovery stage page (#11998): the threats using the most techniques, and the
# sectors and countries those threats target. Distributions capped by ``limit``
# (a top-N list, never a total), so the 100-bucket aggregation cap does not bite.
_THREAT_TYPES = ["Intrusion-Set", "Malware", "Campaign", "Threat-Actor-Group"]
_OCTI_DISCOVERY_TOPS = """
query CtemDiscoveryTops($threats: [String]) {
  topThreats: stixRelationshipsDistribution(relationship_type: ["uses"], fromTypes: $threats, toTypes: ["Attack-Pattern"], field: "internal_id", isTo: false, operation: count, limit: 8) {
    label value entity { ... on StixDomainObject { entity_type } ... on IntrusionSet { name } ... on Malware { name } ... on Campaign { name } ... on ThreatActorGroup { name } }
  }
  sectors: stixRelationshipsDistribution(relationship_type: ["targets"], fromTypes: $threats, toTypes: ["Sector"], field: "internal_id", isTo: true, operation: count, limit: 5) {
    label value entity { ... on Sector { name } }
  }
  countries: stixRelationshipsDistribution(relationship_type: ["targets"], fromTypes: $threats, toTypes: ["Country"], field: "internal_id", isTo: true, operation: count, limit: 5) {
    label value entity { ... on Country { name } }
  }
}
"""

# Sightings = how often tracked indicators were actually OBSERVED in the
# environment - real activity vs theoretical intel (Prioritization signal).
_OCTI_SIGHTINGS = """
query CtemSightings {
  stixSightingRelationshipsNumber(fromTypes: ["Indicator"]) { total }
}
"""

# Priority Intelligence Requirements (PIR) - Enterprise Edition. Counts the
# active intelligence requirements that focus collection/prioritization. Errors
# (no EE / no PIRAPI capability) are tolerated and the fact is simply omitted.
_OCTI_PIRS = """
query CtemPirs { pirs(first: 1) { pageInfo { globalCount } } }
"""

# PIR-flagged threats: intrusion sets + campaigns within a PIR's scope. There is
# no global cross-PIR query, so we fetch the active PIR ids then count flagged
# threats per PIR and sum (entities in several PIRs may be counted more than once).
_OCTI_PIR_IDS = """
query CtemPirIds { pirs(first: 10) { edges { node { id } } } }
"""
_OCTI_PIR_REL = """
query CtemPirRel($pirId: ID!, $types: [String]) {
  pirRelationships(pirId: $pirId, fromTypes: $types, first: 1) {
    pageInfo { globalCount }
  }
}
"""

# Automation playbooks (response automation). ``running`` = enabled playbooks.
# Errors (no AUTOMATION capability) are tolerated and the facts are omitted.
_OCTI_PLAYBOOKS = """
query CtemPlaybooks {
  all: playbooks(first: 1) { pageInfo { globalCount } }
  running: playbooks(first: 1, filters: {mode: and, filters: [{key: ["playbook_running"], values: ["true"]}], filterGroups: []}) { pageInfo { globalCount } }
}
"""

_OCTI_TOP_VULNS = """
query CtemTopVulns {
  vulnerabilities(first: 10, orderBy: x_opencti_cvss_base_score, orderMode: desc) {
    edges { node {
      id name x_opencti_cvss_base_score x_opencti_cvss_base_severity
      x_opencti_epss_score x_opencti_cisa_kev
    } }
  }
}
"""

# Software observables matching the organization profile's tech stack (by CPE or
# product name), the anchor of the tailored KEV count (Software -has-> Vulnerability).
_OCTI_STACK_SOFTWARE = """
query CtemStackSoftware($filters: FilterGroup, $first: Int) {
  stixCyberObservables(types: ["Software"], filters: $filters, first: $first) {
    edges { node { id ... on Software { name vendor cpe } } }
  }
}
"""
_STACK_SOFTWARE_CAP = 500
# Most matched software ids a funnel layer link carries in its OpenCTI filter.
_FUNNEL_LINK_IDS_CAP = 50

# Diagnostic: resolve the acting identity behind the per-user JWT (a user with no
# allowed markings only sees unmarked data, so counts collapse to 0).
_OCTI_ME = """
query CtemMe {
  me {
    user_email name external
    allowed_marking { definition }
    capabilities { name }
    objectOrganization { edges { node { name } } }
  }
}
"""


# ── Generic OpenCTI helpers ──────────────────────────────────────────────────
async def _log_octi_identity(
    client: httpx.AsyncClient, base_url: str, headers: dict[str, str], user_id: str
) -> None:
    me_data, me_err = await _graphql(client, base_url, headers, _OCTI_ME)
    if me_err:
        logger.info("CTEM OpenCTI 'me' unavailable for user %s: %s", user_id, me_err)
        return
    me = (me_data or {}).get("me") or {}
    markings = [m.get("definition") for m in (me.get("allowed_marking") or []) if m.get("definition")]
    caps = [c.get("name") for c in (me.get("capabilities") or []) if c.get("name")]
    orgs = [
        (e.get("node") or {}).get("name")
        for e in ((me.get("objectOrganization") or {}).get("edges") or [])
        if e.get("node")
    ]
    logger.info(
        "CTEM OpenCTI identity for user %s: resolved=%s external=%s bypass=%s "
        "orgs=%s allowed_markings=%d capabilities=%d",
        user_id,
        me.get("user_email"),
        me.get("external"),
        "BYPASS" in caps,
        orgs or "none",
        len(markings),
        len(caps),
    )


async def _octi_multi_counts(
    client: httpx.AsyncClient,
    base_url: str,
    headers: dict[str, str],
    specs: list[tuple[str, list[str], dict | None]],
    user_id: str,
) -> dict[str, int]:
    """Run one multi-number call for *specs* and return ``{key: total}``."""
    params = [
        {"types": types, **({"filters": filt} if filt else {})}
        for (_key, types, filt) in specs
    ]
    data, err = await _graphql(client, base_url, headers, _OCTI_MULTI, {"params": params})
    if err:
        logger.info("CTEM OpenCTI counts unavailable for user %s: %s", user_id, err)
        return {}
    numbers = (data or {}).get("entities") or []
    return {
        key: _to_int((number or {}).get("total"))
        for (key, _t, _f), number in zip(specs, numbers, strict=False)
    }


async def _octi_obs_total(
    client: httpx.AsyncClient, base_url: str, headers: dict[str, str]
) -> int:
    data, err = await _graphql(client, base_url, headers, _OCTI_OBS_TOTAL)
    if err:
        return 0
    return _to_int(((data or {}).get("observables") or {}).get("total"))


async def _octi_rel_count(
    client: httpx.AsyncClient,
    base_url: str,
    headers: dict[str, str],
    *,
    rel: list[str] | None = None,
    frm: list[str] | None = None,
    to: list[str] | None = None,
) -> int:
    data, err = await _graphql(
        client, base_url, headers, _OCTI_REL_NUMBER, {"rel": rel, "from": frm, "to": to}
    )
    if err:
        return 0
    return _to_int(((data or {}).get("stixRelationshipsNumber") or {}).get("total"))


async def _octi_attr_dist(
    client: httpx.AsyncClient,
    base_url: str,
    headers: dict[str, str],
    types: list[str],
    field: str,
) -> list[tuple[str, int]]:
    data, err = await _graphql(
        client, base_url, headers, _OCTI_ATTR_DIST, {"types": types, "field": field}
    )
    if err:
        return []
    return [
        (d.get("label"), _to_int(d.get("value")))
        for d in ((data or {}).get("dist") or [])
        if d.get("label")
    ]


async def _octi_case_closure(
    client: httpx.AsyncClient,
    base_url: str,
    headers: dict[str, str],
    types: list[str],
) -> tuple[int, int]:
    """Return ``(cases_total, cases_closed)`` from a case-status distribution."""
    data, err = await _graphql(
        client, base_url, headers, _OCTI_CASE_STATUS, {"types": types}
    )
    if err:
        return 0, 0
    total = 0
    closed = 0
    for item in (data or {}).get("caseStatus") or []:
        value = _to_int(item.get("value"))
        total += value
        name = ((item.get("entity") or {}).get("template") or {}).get("name") or ""
        if any(token in name.lower() for token in _CASE_CLOSED_TOKENS):
            closed += value
    return total, closed


async def _octi_sightings(
    client: httpx.AsyncClient, base_url: str, headers: dict[str, str]
) -> int | None:
    """Total indicator sightings (observed activity), or ``None`` if unavailable."""
    data, err = await _graphql(client, base_url, headers, _OCTI_SIGHTINGS)
    if err:
        return None
    return _to_int(((data or {}).get("stixSightingRelationshipsNumber") or {}).get("total"))


async def _octi_pirs(
    client: httpx.AsyncClient, base_url: str, headers: dict[str, str]
) -> int | None:
    """Count Priority Intelligence Requirements (EE). ``None`` when unavailable."""
    data, err = await _graphql(client, base_url, headers, _OCTI_PIRS)
    if err:
        return None
    return _to_int((((data or {}).get("pirs") or {}).get("pageInfo") or {}).get("globalCount"))


async def _octi_pir_threats(
    client: httpx.AsyncClient, base_url: str, headers: dict[str, str]
) -> int | None:
    """Threat entities (intrusion sets, campaigns, threat-actor groups &
    individuals) flagged by PIRs (EE) - the adversaries inside the active
    intelligence-requirement scope. Sums per-PIR counts over up to 10 PIRs (an
    entity in several PIRs may count more than once); ``None`` when PIRs are
    unavailable (no EE / PIRAPI capability)."""
    data, err = await _graphql(client, base_url, headers, _OCTI_PIR_IDS)
    if err:
        return None
    edges = (((data or {}).get("pirs") or {}).get("edges")) or []
    pir_ids = [e["node"]["id"] for e in edges if e.get("node") and e["node"].get("id")]
    if not pir_ids:
        return 0
    total = 0
    for pid in pir_ids:
        d, e = await _graphql(
            client,
            base_url,
            headers,
            _OCTI_PIR_REL,
            {
                "pirId": pid,
                "types": [
                    "Intrusion-Set",
                    "Campaign",
                    "Threat-Actor-Group",
                    "Threat-Actor-Individual",
                ],
            },
        )
        if e:
            continue
        total += (
            _to_int(
                (((d or {}).get("pirRelationships") or {}).get("pageInfo") or {}).get(
                    "globalCount"
                )
            )
            or 0
        )
    return total


async def _octi_threat_tech_count(
    client: httpx.AsyncClient, base_url: str, headers: dict[str, str]
) -> int | None:
    """Distinct ATT&CK techniques used by tracked threats (UNCAPPED).

    Counts Attack-Pattern entities that are the target of a "uses" relationship
    via ``stixCoreObjectsNumber.total`` (unbounded). The old
    ``stixRelationshipsDistribution`` approach saturated at 100 because OpenCTI
    caps every distribution aggregation at 100 buckets (``MAX_AGGREGATION_SIZE``).
    ``None`` when unavailable, so the caller falls back to the (capped) tcode set.
    """
    data, err = await _graphql(
        client, base_url, headers, _OCTI_THREAT_TECH_COUNT, {"filters": _F_THREAT_USES}
    )
    if err:
        return None
    return _to_int((((data or {}).get("count")) or {}).get("total"))


async def _octi_threat_tcodes(
    client: httpx.AsyncClient,
    base_url: str,
    headers: dict[str, str],
    tactics: dict[str, int] | None = None,
    tactic_order: dict[str, int] | None = None,
    tactic_ids: dict[str, set[str]] | None = None,
) -> list[str] | None:
    """Full set of ATT&CK T-codes used by tracked threats (UNCAPPED, paginated).

    Walks the ``stixCoreObjects`` connection for Attack-Patterns matching the
    same ``regardingOf`` "uses" filter as ``_octi_threat_tech_count`` and collects
    their MITRE ``x_mitre_id``. Uses a connection query rather than
    ``stixRelationshipsDistribution`` because the latter is hard-capped at 100
    buckets (``MAX_AGGREGATION_SIZE``), which would truncate the set feeding the
    cross-product threat-informed coverage. Returns the sorted T-codes (possibly
    empty), or ``None`` when the FIRST page fails so the caller can fall back to
    the capped distribution query; a later-page failure keeps the partial set
    (still strictly better than the 100-bucket cap).

    *tactics*, when given, is filled with the number of distinct techniques per
    ATT&CK tactic (``mitre-attack`` kill chain phase name) read on the same pages:
    the Discovery heat strip (#11998), no extra query; *tactic_order*, when given,
    with each tactic's kill chain order as OpenCTI stores it (``x_opencti_order``),
    so no client holds a tactic list; *tactic_ids*, when given, with the internal ids
    of each tactic's techniques, so the Validation page can say which of them carry
    an indicator or a detection rule (#11998), no extra query.
    """
    tcodes: set[str] = set()
    seen: set[str] = set()
    cursor: str | None = None
    for _ in range(_OCTI_TCODE_PAGE_CAP):
        data, err = await _graphql(
            client,
            base_url,
            headers,
            _OCTI_THREAT_TECH_CODES,
            {"filters": _F_THREAT_USES, "cursor": cursor},
        )
        if err:
            return sorted(tcodes) if tcodes else None
        conn = (data or {}).get("stixCoreObjects") or {}
        for edge in conn.get("edges") or []:
            node = (edge or {}).get("node") or {}
            code = node.get("x_mitre_id")
            if code:
                tcodes.add(code)
            if tactics is not None and code and code not in seen:
                seen.add(code)
                for phase in node.get("killChainPhases") or []:
                    if (phase or {}).get("kill_chain_name") == "mitre-attack" and phase.get("phase_name"):
                        tactics[phase["phase_name"]] = tactics.get(phase["phase_name"], 0) + 1
                        if tactic_ids is not None and node.get("id"):
                            tactic_ids.setdefault(phase["phase_name"], set()).add(str(node["id"]))
                        if tactic_order is not None and isinstance(phase.get("x_opencti_order"), int):
                            tactic_order.setdefault(phase["phase_name"], phase["x_opencti_order"])
        page = conn.get("pageInfo") or {}
        if not page.get("hasNextPage"):
            break
        cursor = page.get("endCursor")
        if not cursor:
            break
    else:
        # Loop exhausted the page cap while a next page was still advertised (the
        # only way to skip every ``break``) - the T-code set may be truncated.
        # Practically unreachable (ATT&CK has well under the cap's reach), but log
        # it so any future truncation surfaces instead of failing silently.
        logger.warning(
            "CTEM threat T-code pagination hit the %d-page cap with more pages "
            "available; the technique set may be truncated (collected %d).",
            _OCTI_TCODE_PAGE_CAP,
            len(tcodes),
        )
    return sorted(tcodes)


def _dist_rows(rows: list | None) -> list[dict]:
    """``[{name, value}]`` of a distribution, named by its entity, empty names dropped."""
    out: list[dict] = []
    for row in rows or []:
        name = ((row or {}).get("entity") or {}).get("name")
        value = _to_int((row or {}).get("value"))
        if name and value:
            out.append({"name": str(name), "value": value})
    return out


def _named_rows(rows: list[tuple[str, int]], *, limit: int | None = None) -> list[dict[str, str | int]]:
    """``[{name, value}]`` of an attribute distribution, largest first, zeros dropped."""
    ranked = sorted(((str(label), value) for (label, value) in rows if label and value), key=lambda r: -r[1])
    out: list[dict[str, str | int]] = [{"name": name, "value": value} for (name, value) in ranked]
    return out[:limit] if limit else out


async def _octi_discovery_tops(
    client: httpx.AsyncClient, base_url: str, headers: dict[str, str]
) -> dict[str, list[dict]]:
    """Top threats by technique count, and the sectors and countries they target.

    Each list is a top-N (never a total); an unreadable answer is an empty dict, so
    the page shows the cards it can and never a zero presented as a measure.
    """
    data, err = await _graphql(client, base_url, headers, _OCTI_DISCOVERY_TOPS, {"threats": _THREAT_TYPES})
    if err:
        return {}
    data = data or {}
    return {
        "top_threats": _dist_rows(data.get("topThreats")),
        "target_sectors": _dist_rows(data.get("sectors")),
        "target_countries": _dist_rows(data.get("countries")),
    }


def _named_counts(*pairs: tuple[str, Any]) -> list[dict[str, Any]]:
    """``[{name, value}]`` of the counts above 0, in the given order (a viz list, #11998).

    A count a product did not answer (None, a non-int) or at 0 is left out, so a card
    is never drawn on an empty list.
    """
    return [
        {"name": name, "value": value}
        for (name, value) in pairs
        if isinstance(value, int) and not isinstance(value, bool) and value > 0
    ]


def _response_progress(closed: Any, total: Any, playbooks_running: Any, playbooks_total: Any) -> dict[str, int] | None:
    """Closed of the response cases and running of the playbooks, None with neither (#11998).

    Each pair is kept only when its total is a count above 0, so a bar is never drawn
    on an empty queue (an empty queue has no closure rate, as ``case_closure_pct``).
    """

    def _count(v: Any) -> int | None:
        return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None

    out: dict[str, int] = {}
    if (t := _count(total)) and (c := _count(closed)) is not None:
        out.update(cases_closed=min(c, t), cases_total=t)
    if (t := _count(playbooks_total)) and (r := _count(playbooks_running)) is not None:
        out.update(playbooks_running=min(r, t), playbooks_total=t)
    return out or None


def _rule_coverage_by_tactic(
    tactic_ids: dict[str, set[str]], tactic_order: dict[str, int], covered: set[str]
) -> list[dict[str, str | int]]:
    """``[{tactic, total, covered}]`` in kill chain order: each tactic's threat techniques and
    those with an indicator or a detection rule (*covered*, internal ids), empty tactics dropped."""
    rows = sorted(tactic_ids.items(), key=lambda kv: (tactic_order.get(kv[0], 10**6), kv[0]))
    return [{"tactic": tactic, "total": len(ids), "covered": len(ids & covered)} for (tactic, ids) in rows if ids]


async def _octi_playbooks(
    client: httpx.AsyncClient, base_url: str, headers: dict[str, str]
) -> tuple[int, int] | None:
    """Return ``(playbooks_total, playbooks_running)`` or ``None`` if unavailable."""
    data, err = await _graphql(client, base_url, headers, _OCTI_PLAYBOOKS)
    if err:
        return None
    all_count = _to_int((((data or {}).get("all") or {}).get("pageInfo") or {}).get("globalCount"))
    running = _to_int((((data or {}).get("running") or {}).get("pageInfo") or {}).get("globalCount"))
    return all_count, running


async def _octi_detection_coverage(
    client: httpx.AsyncClient, base_url: str, headers: dict[str, str]
) -> tuple[dict, bool]:
    """ATT&CK detection coverage distribution, degrading gracefully across builds.

    Returns ``(cov_data, detects_available)``. Preferred query: non-revoked
    ``indicates`` + ``detects``. On error, retry with the variants that drop the
    least data, in order:
      1. unfiltered ``indicates`` + ``detects`` - build rejects ``dynamicFrom``
         but supports ``detects``; keeps detection-rule coverage (loses the
         non-revoked filter);
      2. non-revoked ``indicates`` only - build rejects ``detects`` but supports
         ``dynamicFrom``; keeps the filter (loses ``detects``);
      3. unfiltered ``indicates`` only - last resort.
    ``detects_available`` tracks whether the surviving query carried ``detects``
    so Validation emits detection-rule coverage only when real.

    Wrapped as one coroutine so the whole fallback chain can be fanned out
    concurrently with the other OpenCTI queries.
    """
    cov_vars = {"activeIndicator": _F_VALID_IND}
    cov_data, cov_err = await _graphql(client, base_url, headers, _OCTI_COVERAGE, cov_vars)
    detects_available = not cov_err
    if cov_err:
        cov_data, cov_err = await _graphql(
            client, base_url, headers, _OCTI_COVERAGE_UNFILTERED_DETECTS
        )
        detects_available = not cov_err
    if cov_err:
        cov_data, cov_err = await _graphql(
            client, base_url, headers, _OCTI_COVERAGE_FALLBACK, cov_vars
        )
    if cov_err:
        cov_data, _ = await _graphql(client, base_url, headers, _OCTI_COVERAGE_UNFILTERED)
    return cov_data or {}, detects_available


async def _octi_top_vulns(
    client: httpx.AsyncClient, base_url: str, headers: dict[str, str]
) -> dict:
    """Top vulnerabilities by CVSS (display payload), ``{}`` when unavailable."""
    data, _ = await _graphql(client, base_url, headers, _OCTI_TOP_VULNS)
    return data or {}


def _stack_matches(node: dict, stack: list[dict[str, str]]) -> str | None:
    """The stack product a Software observable stands for, or ``None``.

    A CPE equal to the entry's wins; otherwise the product name must match
    (case-insensitive) and, when both sides name a vendor, the vendor too.
    """
    cpe = (node.get("cpe") or "").strip().lower()
    name = (node.get("name") or "").strip().lower()
    vendor = (node.get("vendor") or "").strip().lower()
    for entry in stack:
        if entry["cpe"] and cpe and entry["cpe"].lower() == cpe:
            return entry["label"]
        if entry["product"].lower() == name and (
            not entry["vendor"] or not vendor or entry["vendor"].lower() == vendor
        ):
            return entry["label"]
    return None


async def _octi_kev_tailored(
    client: httpx.AsyncClient,
    base_url: str,
    headers: dict[str, str],
    stack: list[dict[str, str]],
    user_id: str,
) -> dict[str, Any] | None:
    """Known-exploited vulnerabilities restricted to the organization's tech stack.

    Resolves the Software observables matching *stack* (bounded), then counts
    the KEV vulnerabilities related to them. ``None`` when OpenCTI cannot answer
    (the card then stays generic), ``{"kev_tailored_count": 0, ...}`` when the
    stack matches no software or no known-exploited vulnerability.
    """
    cpes = sorted({e["cpe"] for e in stack if e["cpe"]})
    names = sorted({e["product"] for e in stack})
    filters = [{"key": ["name"], "values": names}]
    if cpes:
        filters.append({"key": ["cpe"], "values": cpes})
    data, err = await _graphql(
        client,
        base_url,
        headers,
        _OCTI_STACK_SOFTWARE,
        {"filters": _fg(filters, mode="or"), "first": _STACK_SOFTWARE_CAP},
    )
    if err:
        logger.info("CTEM OpenCTI stack software unavailable for user %s: %s", user_id, err)
        return None
    ids: list[str] = []
    matched: list[str] = []
    for edge in ((data or {}).get("stixCyberObservables") or {}).get("edges") or []:
        node = (edge or {}).get("node") or {}
        label = _stack_matches(node, stack)
        if label and node.get("id"):
            ids.append(node["id"])
            if label not in matched:
                matched.append(label)
    if not ids:
        return {
            "kev_tailored_count": 0,
            "kev_tailored_products": [],
            "funnel_tailored": {"relevant": 0, "exploitable": 0},
        }
    regarding = {
        "key": ["regardingOf"],
        "values": [
            {"key": "relationship_type", "values": ["has"]},
            {"key": "id", "values": ids},
        ],
    }
    spec = [
        ("kev_tailored", ["Vulnerability"], _fg([{"key": ["x_opencti_cisa_kev"], "values": ["true"]}, regarding])),
        # The funnel's tailored layers (#11546): every vulnerability of the
        # matched software, then the exploitable ones (KEV or EPSS above the
        # threshold) among them.
        ("relevant", ["Vulnerability"], _fg([regarding])),
        (
            "exploitable_relevant",
            ["Vulnerability"],
            {"mode": "and", "filters": [regarding], "filterGroups": [_F_EXPLOITABLE]},
        ),
    ]
    counts = await _octi_multi_counts(client, base_url, headers, spec, user_id)
    if "kev_tailored" not in counts:
        return None
    # Stack order (the profile's), so the why line names what the reader listed first.
    order = {e["label"]: i for i, e in enumerate(stack)}
    matched.sort(key=lambda label: order.get(label, len(order)))
    funnel = None
    if "relevant" in counts and "exploitable_relevant" in counts:
        # The lists the layers count, opened in OpenCTI from the stage page; a
        # stack matching more software than a link can carry links unfiltered.
        linkable = len(ids) <= _FUNNEL_LINK_IDS_CAP
        funnel = {
            "relevant": counts["relevant"],
            "exploitable": counts["exploitable_relevant"],
            "filters": {
                "relevant": spec[1][2] if linkable else None,
                "exploitable": spec[2][2] if linkable else None,
            },
        }
    return {
        "kev_tailored_count": counts["kev_tailored"],
        "kev_tailored_products": matched[:3],
        "funnel_tailored": funnel,
    }


async def _profile_tech_stack(db: AsyncSession | None) -> list[dict[str, str]]:
    """The primary organization profile's tech stack, ``[]`` when none is set."""
    if db is None:
        return []
    from app.services import organization_context as org_context

    try:
        row = await org_context.current_version(db)
        if row is None:
            return []
        profile = org_context.parse_profile(row.profile)
    except Exception as exc:  # a bad stored profile never breaks the posture
        logger.info("CTEM organization profile unreadable: %s", format_exception_message(exc))
        return []
    stack: list[dict[str, str]] = []
    for entry in profile.tech_stack:
        product = entry.product.strip()
        if not product:
            continue
        vendor = entry.vendor.strip()
        stack.append(
            {
                "product": product,
                "vendor": vendor,
                "cpe": entry.cpe.strip(),
                "version": entry.version.strip(),
                "label": f"{vendor} {product}" if vendor and not product.lower().startswith(vendor.lower()) else product,
            }
        )
    return stack


async def _profile_asset_rows(db: AsyncSession | None) -> tuple[list[Any], list[Any]] | None:
    """The primary organization profile's ``(assets, asset groups)``, ``None`` when it holds no asset."""
    if db is None:
        return None
    from app.services import organization_context as org_context

    try:
        row, profile = await org_context.current_profile(db)  # assets from the records (#1543)
        if row is None:
            return None
    except Exception as exc:  # a bad stored profile never breaks the posture
        logger.info("CTEM organization profile unreadable: %s", format_exception_message(exc))
        return None
    if not profile.assets_exposure:
        return None
    return list(profile.assets_exposure), list(profile.asset_groups)


def _profile_reachability(rows: tuple[list[Any], list[Any]] | None) -> dict[str, int] | None:
    """The reachability split of the profile *rows* (:func:`_profile_asset_rows`), ``None`` when none is held.

    The Exposed layer of the funnel reads it (:mod:`app.services.asset_reachability`).
    """
    from app.services.asset_reachability import reachability_split

    return reachability_split(*rows) if rows else None


#: Most OpenAEV CVE findings the Exposed join reads; past it the join is not made (an asset unread
#: could keep a vulnerability in, so a partial read never drops one).
_EXPOSED_FINDING_CAP = 2000
_EXPOSED_PAGE = 500
#: CVE names per OpenCTI count of the Exposed join (one multi-count call carries every chunk).
_EXPOSED_NAME_CHUNK = 200


def _exposed_basis(inputs: dict[str, Any], tailored: dict[str, Any]) -> tuple[int, dict[str, Any] | None] | None:
    """The funnel's last live layer before Exposed: ``(count, OpenCTI filter)``, ``None`` without a countable filter.

    The same choice :func:`~app.services.ctem_funnel.build_funnel` makes:
    the tailored layers when the profile has a tech stack, else the known ones.
    A known-layer filter of ``None`` means every vulnerability.
    """

    def count(value: Any) -> int | None:
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    if count(tailored.get("relevant")) is not None:
        links = tailored.get("filters") or {}
        for key in ("exploitable", "relevant"):
            layer_count = count(tailored.get(key))
            if layer_count is not None:
                layer_filter = links.get(key)
                return (layer_count, layer_filter) if layer_filter else None
        return None
    exploitable = count(inputs.get("exploitable"))
    if exploitable is not None:
        return exploitable, _F_EXPLOITABLE
    known = count(inputs.get("known"))
    return (known, None) if known is not None else None


async def _aev_cve_assets(
    base_url: str, headers: dict[str, str], client: httpx.AsyncClient
) -> dict[str, list[dict[str, str]]] | None:
    """Every OpenAEV CVE finding as CVE name -> the assets it was found on, ``None`` when unread or past the cap."""
    by_cve: dict[str, list[dict[str, str]]] = {}
    read = 0
    page = 0
    while True:
        resp, err = await _api(
            base_url,
            headers,
            "post",
            "/api/findings/search",
            "search CVE findings",
            json=_search_body(page=page, size=_EXPOSED_PAGE, filter_group=_AEV_CVE_FILTER),
            client=client,
        )
        if err or resp is None:
            return None
        body = resp.json() or {}
        content = [f for f in body.get("content") or [] if isinstance(f, dict)]
        read += len(content)
        if read > _EXPOSED_FINDING_CAP or int(body.get("totalElements") or 0) > _EXPOSED_FINDING_CAP:
            return None
        for finding in content:
            cve = str(finding.get("finding_value") or "").strip().upper()
            if not cve.startswith("CVE-"):
                continue
            refs = by_cve.setdefault(cve, [])
            for asset in finding.get("finding_assets") or []:
                if isinstance(asset, dict) and (asset.get("asset_id") or asset.get("asset_name")):
                    refs.append(
                        {
                            "source": "openaev",
                            "external_id": str(asset.get("asset_id") or ""),
                            "name": str(asset.get("asset_name") or ""),
                        }
                    )
        page += 1
        if not content or page >= int(body.get("totalPages") or 0):
            return by_cve


async def _octi_named_count(
    client: httpx.AsyncClient,
    base_url: str,
    headers: dict[str, str],
    layer_filter: dict[str, Any] | None,
    names: list[str],
    user_id: str,
) -> int | None:
    """How many vulnerabilities of the layer *layer_filter* counts carry one of *names*, ``None`` when unread."""
    specs: list[tuple[str, list[str], dict[str, Any] | None]] = []
    for start in range(0, len(names), _EXPOSED_NAME_CHUNK):
        named = {"key": ["name"], "values": names[start : start + _EXPOSED_NAME_CHUNK]}
        specs.append(
            (
                f"chunk{start}",
                ["Vulnerability"],
                {"mode": "and", "filters": [named], "filterGroups": [layer_filter] if layer_filter else []},
            )
        )
    counts = await _octi_multi_counts(client, base_url, headers, specs, user_id)
    if any(key not in counts for key, _t, _f in specs):
        return None
    return sum(counts.values())


async def _exposed_join(
    creds: dict[str, tuple[str, dict[str, str]] | None],
    stage_facts: dict[str, dict[str, Any]],
    present: set[str],
    rows: tuple[list[Any], list[Any]] | None,
    tailored: Any,
    user_id: str,
) -> int | None:
    """The Exposed layer's count: the last live layer's vulnerabilities left on an exposed asset (#12351).

    The join: OpenAEV's CVE findings name the endpoints each CVE was found
    on; a CVE whose every endpoint is proven unreachable in the profile
    (:func:`~app.services.asset_reachability.unreachable_keys`) is counted
    once in the last live layer through OpenCTI (its name under that layer's
    filter) and taken off. Everything else stays in (rule 1). ``None`` when
    the join cannot be made: no profile asset, OpenCTI or OpenAEV not read,
    findings past the cap, a layer without a countable filter.
    """
    if rows is None or not {"opencti", "openaev"} <= present:
        return None
    octi, aev = creds.get("opencti"), creds.get("openaev")
    inputs = (stage_facts.get("prioritization") or {}).get("funnel_inputs")
    if not octi or not aev or not inputs:
        return None
    basis = _exposed_basis(inputs, (tailored.get("funnel_tailored") if isinstance(tailored, dict) else None) or {})
    if basis is None:
        return None
    layer_count, layer_filter = basis
    from app.services.asset_reachability import unreachable_keys

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            by_cve = await _aev_cve_assets(aev[0], aev[1], client)
            if by_cve is None:
                return None
            dropped_names = unreachable_keys(by_cve, *rows)
            if not dropped_names:
                return layer_count
            dropped = await _octi_named_count(client, octi[0], octi[1], layer_filter, dropped_names, user_id)
    except Exception as exc:  # the join is best-effort: Exposed then stays not available
        logger.warning("CTEM exposed join error (user %s): %s", user_id, format_exception_message(exc))
        return None
    return None if dropped is None else max(0, layer_count - dropped)


# ── Cyber collector ──────────────────────────────────────────────────────────
async def _collect_opencti_cyber(
    base_url: str, headers: dict[str, str], user_id: str, client: httpx.AsyncClient
) -> dict[str, dict]:
    """Gather per-stage OpenCTI cyber facts for *user_id* (live, scoped).

    Every independent OpenCTI query is fanned out concurrently (bounded by
    ``_OCTI_MAX_CONCURRENCY``) instead of run one-after-another, which was the
    dominant cost of a live posture build. Queries that are internally sequential
    by nature (T-code pagination, the coverage fallback chain, per-PIR counts)
    each run as a single unit that overlaps with the rest.
    """
    stages: dict[str, dict] = {}
    # Distinct threat techniques per ATT&CK tactic, filled by the T-code walk.
    tactic_counts: dict[str, int] = {}
    tactic_order: dict[str, int] = {}
    # The internal ids of each tactic's threat techniques, for Validation's rule coverage.
    tactic_ids: dict[str, set[str]] = {}

    (
        _identity,
        counts,
        observables_total,
        severity_rows,
        case_closure,
        sightings_total,
        pirs_total,
        pir_threats,
        playbooks,
        coverage,
        threat_tech_count,
        threat_tcodes,
        top_data,
        discovery_tops,
        observable_type_rows,
    ) = await _gather_bounded(
        _OCTI_MAX_CONCURRENCY,
        _log_octi_identity(client, base_url, headers, user_id),
        _octi_multi_counts(client, base_url, headers, _CYBER_COUNT_SPECS, user_id),
        _octi_obs_total(client, base_url, headers),
        _octi_attr_dist(
            client, base_url, headers, ["Vulnerability"], "x_opencti_cvss_base_severity"
        ),
        # Response workflow = incident-response + request-for-information cases.
        _octi_case_closure(client, base_url, headers, ["Case-Incident", "Case-Rfi"]),
        _octi_sightings(client, base_url, headers),
        _octi_pirs(client, base_url, headers),
        _octi_pir_threats(client, base_url, headers),
        _octi_playbooks(client, base_url, headers),
        _octi_detection_coverage(client, base_url, headers),
        _octi_threat_tech_count(client, base_url, headers),
        _octi_threat_tcodes(client, base_url, headers, tactic_counts, tactic_order, tactic_ids),
        _octi_top_vulns(client, base_url, headers),
        _octi_discovery_tops(client, base_url, headers),
        # The Scoping page's mixed views (#11998): the observables in scope by type.
        _octi_attr_dist(client, base_url, headers, ["Stix-Cyber-Observable"], "entity_type"),
    )
    counts["observables"] = observables_total
    cases_total, cases_closed = case_closure
    cov_data, detects_available = coverage
    # Uncapped T-code set (paginated connection query). Falls back to the capped
    # (<=100) distribution only if the paginated fetch fully fails, so the
    # cross-product threat-informed coverage stays best-effort but is no longer
    # silently truncated at 100 on threat-rich tenants.
    if threat_tcodes is None:
        threat_tech_data, _ = await _graphql(client, base_url, headers, _OCTI_THREAT_TECH)
        threat_tcodes = sorted(
            {
                (r.get("entity") or {}).get("x_mitre_id")
                for r in (threat_tech_data or {}).get("threatTech") or []
                if (r.get("entity") or {}).get("x_mitre_id")
            }
        )

    vulns_total = counts.get("vulnerabilities", 0)
    observables_total = counts.get("observables", 0)
    reports_total = counts.get("reports", 0)
    indicators_total = counts.get("indicators", 0)
    scope_dims = [observables_total > 0, vulns_total > 0, reports_total > 0, indicators_total > 0]
    stages["scoping"] = {
        "observables": observables_total,
        "vulnerabilities_total": vulns_total,
        "reports": reports_total,
        "indicators_total": indicators_total,
        # PIRs define the intelligence scope - at least one means collection is
        # focused on declared priorities (EE; omitted when unavailable).
        "pirs": pirs_total,
        "scope_completeness_pct": pct(sum(scope_dims), len(scope_dims)),
        # The Scoping page's mixed views (#11998), nested lists so the history
        # snapshot (scalar facts only) and the stage score read nothing new.
        "observables_by_type": _named_rows(observable_type_rows, limit=8),
        # CVSS severity as OpenCTI stores it (same rows as Prioritization's distribution).
        "vulnerabilities_by_severity": [{"label": label, "value": value} for (label, value) in severity_rows if value],
    }

    indicated = {r.get("label") for r in (cov_data.get("indicates") or []) if r.get("label")}
    detected = {r.get("label") for r in (cov_data.get("detects") or []) if r.get("label")}
    covered = indicated | detected
    techniques_total = counts.get("attack_patterns", 0)
    stages["discovery"] = {
        "intrusion_sets": counts.get("intrusion_sets", 0),
        "threat_actor_groups": counts.get("threat_actor_groups", 0),
        "campaigns": counts.get("campaigns", 0),
        "malwares": counts.get("malwares", 0),
        "attack_patterns": techniques_total,
        "reports": reports_total,
        "incidents": counts.get("incidents", 0),
        "indicators_total": indicators_total,
        "indicators_valid": counts.get("indicators_valid", 0),
        "indicators_detection": counts.get("indicators_detection", 0),
        "techniques_total": techniques_total,
        "techniques_covered": len(covered),
        "detection_coverage_pct": pct(len(covered), techniques_total),
        # ``threat_techniques`` headline: prefer the uncapped count query, but
        # never report fewer than the distinct T-codes actually enumerated.
        "threat_techniques": max(threat_tech_count or 0, len(threat_tcodes)),
        # Uncapped T-code set (paginated); drives the cross-product threat-informed
        # coverage computed in ``build_posture_for_user``.
        "threat_technique_tcodes": threat_tcodes,
        # The Discovery page's mixed views (#11998), nested lists so the history
        # snapshot (scalar facts only) and the stage score read nothing new.
        "techniques_by_tactic": [
            # In kill chain order as OpenCTI stores it; a phase without an order last.
            {"tactic": tactic, "count": count}
            for tactic, count in sorted(tactic_counts.items(), key=lambda kv: (tactic_order.get(kv[0], 10**6), kv[0]))
        ],
        **discovery_tops,
        # Threat entities (intrusion sets, campaigns, threat-actor groups &
        # individuals) flagged by PIRs - the prioritized adversaries actively
        # tracked against intelligence requirements (EE; omitted when unavailable).
        "pir_threats": pir_threats,
    }

    # When the detects query works, always surface OpenCTI detection-rule coverage
    # (0% when nothing is detected) so Validation has a real score - "unproven"
    # reads as 0, never "--". OpenAEV BAS efficacy overrides this when connected.
    if detects_available:
        stages["validation"] = {
            "detection_rule_coverage_pct": pct(len(detected), techniques_total) or 0.0,
            "techniques_detected": len(detected),
            # The Validation page's mixed views (#11998), nested so the history snapshot
            # (scalar facts only) and the stage score read nothing new: per ATT&CK tactic,
            # the tracked threats' techniques and those with a live indicator or a
            # detection rule in OpenCTI (the same ``covered`` set as Discovery's coverage).
            "rule_coverage_by_tactic": _rule_coverage_by_tactic(tactic_ids, tactic_order, covered),
            # How deep OpenCTI's detection content goes, each figure as OpenCTI counts it;
            # a figure it did not answer is left out.
            "detection_depth": [
                {"name": name, "value": value}
                for (name, value) in (
                    ("indicators", indicators_total),
                    ("detection_indicators", counts.get("indicators_detection")),
                    ("sightings", sightings_total),
                )
                if isinstance(value, int) and not isinstance(value, bool)
            ],
        }

    crit_or_kev = counts.get("critical_or_kev", 0)
    severity = [{"label": label, "value": value} for (label, value) in severity_rows]
    top_vulns = [
        {
            # The OpenCTI id: the "Open in OpenCTI" pivot of each row (#1357).
            "id": n.get("id"),
            "name": n.get("name"),
            "cvss": n.get("x_opencti_cvss_base_score"),
            "severity": n.get("x_opencti_cvss_base_severity"),
            "epss": n.get("x_opencti_epss_score"),
            "kev": bool(n.get("x_opencti_cisa_kev")),
        }
        for e in ((top_data or {}).get("vulnerabilities", {}).get("edges") or [])
        if (n := e.get("node"))
    ]
    stages["prioritization"] = {
        "vulnerabilities_total": vulns_total,
        "kev_count": counts.get("kev", 0),
        "critical_count": counts.get("critical", 0),
        "high_epss_count": counts.get("high_epss", 0),
        "critical_or_kev_count": crit_or_kev,
        "severity_distribution": severity,
        "top_vulnerabilities": top_vulns,
        # Indicator sightings (real activity) + Priority Intelligence Requirements
        # ground prioritization in what's actually observed and required, not just
        # theoretical CVSS. EE/permission-gated signals degrade to omitted facts.
        "sightings_total": sightings_total,
        "pirs": pirs_total,
        "noncritical_share_pct": pct(vulns_total - crit_or_kev, vulns_total),
        # The funnel's OpenCTI inputs (#11546), nested so the history snapshot
        # (scalar facts only) and the stage score read nothing new; turned into
        # ``funnel`` by ``build_posture_for_user`` once the tailoring is known.
        "funnel_inputs": {"known": vulns_total, "exploitable": counts.get("exploitable")},
    }

    playbooks_total, playbooks_running = playbooks or (None, None)
    stages["remediation"] = {
        "case_incidents": counts.get("case_incidents", 0),
        "case_rfis": counts.get("case_rfis", 0),
        "cases_status_total": cases_total,
        "cases_closed": cases_closed,
        # Real closure rate, or None when there are NO cases: an empty queue has
        # no closure rate to report, so it must NOT read as a vacuous 100% (which
        # would fake a "strong" Remediation). The score blends in playbook
        # automation + OpenAEV prevention instead.
        "case_closure_pct": pct(cases_closed, cases_total),
        # Response automation: enabled playbooks driving closure without humans.
        "playbooks_total": playbooks_total,
        "playbooks_running": playbooks_running,
        "playbook_automation_pct": pct(playbooks_running or 0, playbooks_total or 0),
        # The Mobilization page's mixed views (#11998), nested so the history snapshot
        # (scalar facts only) and the stage score read nothing new: the response cases
        # by kind and the closure and automation counts OpenCTI already answers above.
        "response_mix": _named_counts(
            ("case_incidents", counts.get("case_incidents")),
            ("case_rfis", counts.get("case_rfis")),
        ),
        "response_progress": _response_progress(cases_closed, cases_total, playbooks_running, playbooks_total),
    }

    logger.info(
        "CTEM OpenCTI cyber for user %s: vulns=%s observables=%s reports=%s indicators=%s "
        "attack_patterns=%s techniques_covered=%s kev=%s cases=%s sightings=%s pirs=%s "
        "pir_threats=%s playbooks=%s",
        user_id, vulns_total, observables_total, reports_total, indicators_total,
        techniques_total, len(covered), counts.get("kev", 0), cases_total,
        sightings_total, pirs_total, pir_threats, playbooks,
    )
    return stages


# ── FIMI collector ───────────────────────────────────────────────────────────
async def _collect_opencti_fimi(
    base_url: str, headers: dict[str, str], user_id: str, client: httpx.AsyncClient
) -> dict[str, dict]:
    """Gather per-stage OpenCTI FIMI/disinformation facts for *user_id* (concurrent)."""
    stages: dict[str, dict] = {}

    (
        _identity,
        counts,
        channel_platforms,
        event_types,
        channels_attributed,
        channels_with_infra,
        case_closure,
    ) = await _gather_bounded(
        _OCTI_MAX_CONCURRENCY,
        _log_octi_identity(client, base_url, headers, user_id),
        _octi_multi_counts(client, base_url, headers, _FIMI_COUNT_SPECS, user_id),
        _octi_attr_dist(client, base_url, headers, ["Channel"], "channel_types"),
        _octi_attr_dist(client, base_url, headers, ["Event"], "event_types"),
        _octi_rel_count(client, base_url, headers, rel=["belongs-to"], frm=["Channel"]),
        _octi_rel_count(
            client, base_url, headers, rel=["publishes"], frm=["Channel"],
            to=["Domain-Name", "Url", "Hostname"],
        ),
        _octi_case_closure(client, base_url, headers, ["Case-Incident", "Case-Rfi"]),
    )
    cases_total, cases_closed = case_closure

    narratives = counts.get("narratives", 0)
    channels = counts.get("channels", 0)
    events = counts.get("events", 0)
    languages = counts.get("languages", 0)
    media = counts.get("media", 0)
    incidents = counts.get("incidents", 0)
    election_events = sum(v for (label, v) in event_types if "election" in (label or "").lower())

    scope_dims = [narratives > 0, channels > 0, events > 0, languages > 0]
    depth_dims = [media > 0, len(channel_platforms) > 0]
    stages["scoping"] = {
        "narratives_total": narratives,
        "channels_total": channels,
        "events_total": events,
        "languages_total": languages,
        "scope_completeness_pct": pct(sum(scope_dims), len(scope_dims)),
    }
    stages["discovery"] = {
        "media_total": media,
        "channel_platforms": len(channel_platforms),
        # Plain count of Narrative entities (the "subnarrative-of" relationship
        # count was near-always 0 and not meaningful).
        "narratives_total": narratives,
        "monitoring_depth_pct": pct(sum(depth_dims), len(depth_dims)),
    }
    stages["prioritization"] = {
        "channels_total": channels,
        "channels_attributed": channels_attributed,
        "election_events": election_events,
        "attribution_pct": pct(channels_attributed, channels),
    }
    stages["validation"] = {
        "channels_with_infra": channels_with_infra,
        "incidents": incidents,
        "corroboration_pct": pct(channels_with_infra, channels),
    }
    stages["remediation"] = {
        "case_incidents": cases_total,
        "incidents": incidents,
        "case_closure_pct": _closure_pct(cases_closed, cases_total),
    }

    logger.info(
        "CTEM OpenCTI fimi for user %s: narratives=%s channels=%s events=%s media=%s "
        "attributed=%s with_infra=%s cases=%s",
        user_id, narratives, channels, events, media,
        channels_attributed, channels_with_infra, cases_total,
    )
    return stages


# ── Fraud collector ──────────────────────────────────────────────────────────
async def _collect_opencti_fraud(
    base_url: str, headers: dict[str, str], user_id: str, client: httpx.AsyncClient
) -> dict[str, dict]:
    """Gather per-stage OpenCTI fraud facts for *user_id* (observables + cases)."""
    stages: dict[str, dict] = {}

    _identity, counts, takedowns = await _gather_bounded(
        _OCTI_MAX_CONCURRENCY,
        _log_octi_identity(client, base_url, headers, user_id),
        _octi_multi_counts(client, base_url, headers, _FRAUD_COUNT_SPECS, user_id),
        _octi_case_closure(client, base_url, headers, ["Case-Rft"]),
    )
    takedowns_total, takedowns_closed = takedowns

    brands = counts.get("brands", 0)
    domains = counts.get("domains", 0)
    urls = counts.get("urls", 0)
    hostnames = counts.get("hostnames", 0)
    personas = counts.get("personas", 0)
    credentials = counts.get("credentials", 0)
    cards = counts.get("cards", 0)
    phishing_infra = counts.get("phishing_infra", 0)
    indicators = counts.get("indicators", 0)
    indicators_valid = counts.get("indicators_valid", 0)
    indicators_detection = counts.get("indicators_detection", 0)
    phishing_assets = domains + urls + hostnames

    scope_dims = [brands > 0, domains > 0, personas > 0]
    discovery_dims = [phishing_assets > 0, credentials > 0, cards > 0, phishing_infra > 0]
    stages["scoping"] = {
        "brands_total": brands,
        "domains_total": domains,
        "personas_total": personas,
        "scope_completeness_pct": pct(sum(scope_dims), len(scope_dims)),
    }
    stages["discovery"] = {
        "phishing_assets": phishing_assets,
        "credentials_total": credentials,
        "cards_total": cards,
        "phishing_infra": phishing_infra,
        "discovery_pct": pct(sum(discovery_dims), len(discovery_dims)),
    }
    # Containment = share of fraud indicators that have been neutralised (revoked).
    stages["prioritization"] = {
        "phishing_infra": phishing_infra,
        "cards_total": cards,
        "credentials_total": credentials,
        "containment_pct": pct(indicators - indicators_valid, indicators),
    }
    # Confirmed-malicious share (validated detections) - OpenAEV prevention, when
    # connected, overrides this in the fraud validation scorer.
    stages["validation"] = {
        "indicators_valid": indicators_valid,
        "confirmed_pct": pct(indicators_detection, indicators),
    }
    stages["remediation"] = {
        "takedown_cases": takedowns_total,
        "takedowns_open": max(takedowns_total - takedowns_closed, 0),
        "case_closure_pct": _closure_pct(takedowns_closed, takedowns_total),
    }

    logger.info(
        "CTEM OpenCTI fraud for user %s: brands=%s domains=%s phishing_assets=%s "
        "creds=%s cards=%s phishing_infra=%s takedowns=%s",
        user_id, brands, domains, phishing_assets, credentials, cards,
        phishing_infra, takedowns_total,
    )
    return stages


# ── OpenAEV (REST) ───────────────────────────────────────────────────────────

def _aev_finished_filter(since_iso: str | None = None) -> dict:
    """FINISHED-simulation search filter, optionally windowed to a start-date floor.

    The date floor (``exercise_start_date >= since_iso``) mirrors the OpenAEV home
    dashboard's time filter for its expectation-result donuts, so the aggregated
    success rates match what the home shows.
    """
    filters: list[dict] = [
        {"key": "exercise_status", "mode": "or", "operator": "eq", "values": ["FINISHED"]}
    ]
    if since_iso:
        filters.append(
            {
                "key": "exercise_start_date",
                "mode": "and",
                "operator": "gte",
                "values": [since_iso],
            }
        )
    return {"mode": "and", "filters": filters}
_AEV_CVE_FILTER = {
    "mode": "and",
    "filters": [{"key": "finding_type", "mode": "or", "operator": "eq", "values": ["cve"]}],
}


async def _aev_total(
    base_url: str,
    headers: dict[str, str],
    path: str,
    body: dict | None = None,
    *,
    client: httpx.AsyncClient | None = None,
) -> int | None:
    """Return ``totalElements`` for a search endpoint (the correct total)."""
    resp, err = await _api(
        base_url, headers, "post", path, f"count {path}",
        json=body or _search_body(size=1), client=client,
    )
    if err or resp is None:
        return None
    return (resp.json() or {}).get("totalElements")


async def _aev_asset_groups_total(
    base_url: str, headers: dict[str, str], client: httpx.AsyncClient
) -> int | None:
    """Count asset groups, resilient to the paginated ``/search`` path failing.

    Prefer ``POST /api/asset_groups/search`` (``totalElements``). On some OpenAEV
    instances that criteria-builder/pagination path errors (or is absent) while
    the plain ``GET /api/asset_groups`` list still works - and both require the
    SAME ``ACCESS_ASSETS`` capability as ``/api/endpoints/search``, so this is NOT
    a permission difference. Without the fallback "Asset groups" stayed on its
    grey sample even though OpenAEV was connected and endpoints resolved live.
    """
    total = await _aev_total(base_url, headers, "/api/asset_groups/search", client=client)
    if total is not None:
        return total
    resp, err = await _api(
        base_url, headers, "get", "/api/asset_groups", "list asset groups", client=client
    )
    if err or resp is None:
        return None
    rows = resp.json()
    return len(rows) if isinstance(rows, list) else None


def _aev_success_pct(buckets: dict[str, int]) -> float | None:
    total = sum(buckets.values())
    return pct(buckets.get("SUCCESS", 0), total) if total else None


def _aev_pending_pct(type_buckets: dict[str, dict[str, int]]) -> float | None:
    """Share of expectation results in the window that are still PENDING.

    The completeness guard for every success rate above it. A PENDING
    expectation has no verdict yet - OpenAEV's own averages EXCLUDE those rows,
    so a window that is mostly pending can report a flattering success rate
    computed from a handful of answered targets. Surfacing the pending share
    lets the posture layer say how much of the validation data is actually
    conclusive instead of implying the rates are complete.
    """
    pending = 0
    total = 0
    for buckets in type_buckets.values():
        pending += buckets.get("PENDING", 0)
        total += sum(buckets.values())
    return pct(pending, total) if total else None


async def _aev_expectations_drift(
    base_url: str,
    headers: dict[str, str],
    client: httpx.AsyncClient,
    exercise_ids: list[str],
) -> int | None:
    """Count sampled finished simulations whose expectations have DRIFTED.

    Drift means an inject's stored expectations no longer match the predefined
    expectations of its injector contract, so the simulation measured something
    other than what today's content would measure. That does not invalidate the
    recorded results, but it does weaken how far they can be trusted as a
    CURRENT posture statement - which is exactly what a CTEM score claims.

    Bounded to ``_AEV_DRIFT_SAMPLE`` simulations: this is one request per
    simulation, and the posture build already fans out a dozen calls.
    ``None`` when nothing could be read (older build, or no permission), so the
    caller can omit the fact rather than report a misleading zero.
    """
    ids = [i for i in exercise_ids if i][:_AEV_DRIFT_SAMPLE]
    if not ids:
        return None
    results = await _gather_bounded(
        _AEV_MAX_CONCURRENCY,
        *(
            _api(
                base_url, headers, "get",
                f"/api/exercises/{eid}/expectations-drift", "expectations drift",
                client=client,
            )
            for eid in ids
        ),
    )
    drifted = 0
    seen = False
    for resp, err in results:
        if err or resp is None:
            continue
        payload = resp.json() or {}
        if not isinstance(payload, dict):
            continue
        seen = True
        if payload.get("drift_detected"):
            drifted += 1
    return drifted if seen else None


async def _aev_collectors_active(
    base_url: str, headers: dict[str, str], client: httpx.AsyncClient
) -> int | None:
    """Count detection collectors that are running / recently executed."""
    resp, err = await _api(
        base_url, headers, "get", "/api/collectors?include_next=true",
        "list collectors", client=client,
    )
    if err or resp is None:
        return None
    rows = resp.json() or []
    if not isinstance(rows, list):
        return None
    active = 0
    for collector in rows:
        instance = collector.get("connector_instance") or {}
        if instance.get("connector_instance_current_status") == "started" or collector.get(
            "collector_last_execution"
        ):
            active += 1
    return active


async def _aev_attack_coverage(
    base_url: str, headers: dict[str, str], client: httpx.AsyncClient
) -> list[dict] | None:
    """Global ATT&CK coverage matrix across the latest finished simulations.

    Uses the dedicated ``GET /api/attack_patterns/coverage`` endpoint (per-technique
    prevention/detection across simulations). Returns ``None`` on older OpenAEV
    versions that lack the endpoint, so the caller falls back to the per-exercise
    ``results-by-attack-patterns`` scan.
    """
    # No ?latest filter: aggregate across ALL simulations so the numbers match
    # exactly what the OpenAEV home security-coverage matrix shows (ES-backed).
    resp, err = await _api(
        base_url, headers, "get", "/api/attack_patterns/coverage",
        "att&ck coverage matrix", client=client,
    )
    if err or resp is None:
        return None
    rows = resp.json()
    return rows if isinstance(rows, list) else None


# Core OpenAEV attack-surface domains - the validation "security domain power"
# the OpenAEV home highlights. ``Tabletop`` and ``To classify`` are intentionally
# excluded: they are not attack-surface vectors.
_AEV_CORE_DOMAINS = (
    "Endpoint",
    "Network",
    "Web App",
    "E-mail Infiltration",
    "Data Exfiltration",
    "URL Filtering",
    "Cloud",
)


async def _aev_domains(
    base_url: str, headers: dict[str, str], client: httpx.AsyncClient
) -> dict[str, str] | None:
    """Map ``{domain_id: domain_name}`` from OpenAEV, or ``None`` on failure.

    Required because ``/api/injector_contracts/domain-counts`` groups by the
    Domain entity *id* - a random, per-instance UUID (OpenAEV seeds domains with
    ``gen_random_uuid()`` in migration V4_58), NOT the display name the count
    endpoint's ``@Schema`` example misleadingly suggests. So the id -> name map is
    needed to resolve which counts belong to the core attack-surface domains.
    """
    resp, err = await _api(
        base_url, headers, "get", "/api/domains", "list domains", client=client
    )
    if err or resp is None:
        return None
    rows = resp.json()
    if not isinstance(rows, list):
        return None
    return {
        d["domain_id"]: str(d.get("domain_name", ""))
        for d in rows
        if isinstance(d, dict) and d.get("domain_id")
    }


async def _aev_domain_coverage(
    base_url: str, headers: dict[str, str], client: httpx.AsyncClient
) -> tuple[int | None, int, list[dict[str, Any]] | None]:
    """How many core attack-surface domains have at least one injector contract.

    The third value is the per-domain breakdown the Scoping ring draws
    (``[{name, covered, contracts}]`` in the core-domain order), or ``None``
    whenever ``covered`` is ``None``: no segment is drawn from unresolved rows.

    Agent-agnostic breadth of the validated attack surface (the same domains the
    OpenAEV home "security domain power" panel shows). ``domain-counts`` groups by
    the Domain *id* (a random per-instance UUID - see ``_aev_domains``), so we
    resolve id -> name via ``/api/domains`` (fetched concurrently) and count the
    core domains BY NAME that have content. Returns ``(covered, total)`` where
    ``total`` is the fixed core-domain count and ``covered`` is ``None`` when the
    data cannot be resolved (so the derived fact degrades to its sample
    placeholder instead of a misleading 0).
    """
    total = len(_AEV_CORE_DOMAINS)
    core = {d.casefold() for d in _AEV_CORE_DOMAINS}
    id_to_name, counts_result = await asyncio.gather(
        _aev_domains(base_url, headers, client),
        _api(
            base_url, headers, "post", "/api/injector_contracts/domain-counts",
            "injector contract domain counts", json=_search_body(size=1), client=client,
        ),
    )
    resp, err = counts_result
    if err or resp is None:
        return None, total, None
    rows = resp.json() or []
    if not isinstance(rows, list):
        return None, total, None

    resolved = bool(id_to_name)  # a usable id -> name map is available
    covered: set[str] = set()
    contracts: dict[str, int] = {}
    saw_unresolved = False
    for row in rows:
        if not isinstance(row, dict) or _to_int(row.get("count")) <= 0:
            continue
        # ``domain`` is the grouped Domain id; map it to its display name. Tolerate
        # a build that returns the name directly by falling back to the raw value.
        raw = str(row.get("domain", ""))
        name = id_to_name.get(raw, "") if resolved else raw
        if name.casefold() in core:
            covered.add(name.casefold())
            contracts[name.casefold()] = contracts.get(name.casefold(), 0) + _to_int(row.get("count"))
        elif not resolved:
            # No id -> name map and this value is not itself a core domain name:
            # it is almost certainly an unresolved domain UUID.
            saw_unresolved = True
    # Never report a misleading 0 from rows we could not resolve to names.
    if not resolved and not covered and saw_unresolved:
        return None, total, None
    breakdown = [
        {"name": d, "covered": d.casefold() in covered, "contracts": contracts.get(d.casefold(), 0)}
        for d in _AEV_CORE_DOMAINS
    ]
    return len(covered), total, breakdown


async def _aev_kill_chain_phases(
    base_url: str, headers: dict[str, str], client: httpx.AsyncClient
) -> list[dict[str, Any]] | None:
    """OpenAEV's kill chain phases (``phase_id``, ``phase_name``, ``phase_order``,
    ``phase_kill_chain_name``), or ``None`` when they cannot be read.

    The coverage rows name each technique's phases but not the kill chain a
    phase belongs to: this list names the frameworks the tenant's data carries
    (MITRE ATT&CK, ATLAS, DISARM, a custom one), so no framework is a constant.
    """
    resp, err = await _api(
        base_url, headers, "get", "/api/kill_chain_phases", "kill chain phases", client=client
    )
    if err or resp is None:
        return None
    rows = resp.json()
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else None


# Bounds of the Validation outcome matrix payload, the same as the custom
# dashboard's outcome widgets (``dashboard_live.MAX_OUTCOMES`` and friends).
_MATRIX_MAX_ITEMS = 600
_MATRIX_MAX_CHAINS = 10
_MATRIX_MAX_GROUPS = 40


def _matrix_outcome(prev_ok: int, prev_total: int, det_ok: int, det_total: int) -> str:
    """One outcome per exercised technique: prevented, detected (only), missed or untested."""
    if prev_ok > 0:
        return "prevented"
    if det_ok > 0:
        return "detected"
    if prev_total > 0 or det_total > 0:
        return "missed"
    return "untested"


def _outcome_matrix(
    rows: list[dict] | None, phases: list[dict[str, Any]] | None
) -> dict[str, Any] | None:
    """The Validation hero payload: exercised techniques by kill chain phase, any framework.

    Shape of the custom dashboard ``outcomes`` data (``chains`` of phases in
    ``phase_order``, ``items`` with their ``phases``), each item carrying its
    prevention and detection counts and its ``outcome``. A chain lists its
    exercised-technique ``count``; the ``default`` chain is the one with the most
    exercised techniques (a tie goes to more phases, then the name). Kill chain
    names and phase labels come from OpenAEV as written. ``None`` when the
    coverage matrix could not be read: the hero shows "not configured".
    """
    if rows is None:
        return None
    phase_by_id: dict[str, dict[str, Any]] = {}
    for p in phases or []:
        pid = p.get("phase_id")
        if pid:
            phase_by_id[str(pid)] = {
                "chain": str(p.get("phase_kill_chain_name") or ""),
                "label": str(p.get("phase_name") or pid),
                "order": _to_int(p.get("phase_order")),
            }
    items: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict) or len(items) >= _MATRIX_MAX_ITEMS:
            continue
        tid = row.get("attack_pattern_id")
        if not tid:
            continue
        row_phases: list[str] = []
        for kp in row.get("kill_chain_phases") or []:
            if not isinstance(kp, dict) or not kp.get("phase_id"):
                continue
            pid = str(kp["phase_id"])
            row_phases.append(pid)
            # A phase the list did not return is still drawn, under an unnamed chain.
            phase_by_id.setdefault(
                pid,
                {"chain": "", "label": str(kp.get("phase_name") or pid), "order": _to_int(kp.get("phase_order"))},
            )
        ps, pt = _to_int(row.get("prevention_success")), _to_int(row.get("prevention_total"))
        ds, dt = _to_int(row.get("detection_success")), _to_int(row.get("detection_total"))
        items.append(
            {
                "id": str(tid),
                "label": str(row.get("attack_pattern_name") or row.get("attack_pattern_external_id") or tid),
                "code": str(row.get("attack_pattern_external_id") or ""),
                "phases": row_phases,
                "success": ps,
                "failed": max(pt - ps, 0),
                "pending": 0,
                "parts": [],
                "prevention": {"success": ps, "total": pt},
                "detection": {"success": ds, "total": dt},
                "outcome": _matrix_outcome(ps, pt, ds, dt),
            }
        )
    by_chain: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for pid, phase in phase_by_id.items():
        by_chain.setdefault(phase["chain"], []).append((pid, phase))
    chains: list[dict[str, Any]] = []
    for name, members in by_chain.items():
        ids = {pid for pid, _ in members}
        count = sum(1 for item in items if ids.intersection(item["phases"]))
        ordered = sorted(members, key=lambda m: (m[1]["order"], m[1]["label"].casefold()))
        chains.append(
            {
                "id": name,
                "label": name,
                "count": count,
                "groups": [{"id": pid, "label": phase["label"]} for pid, phase in ordered[:_MATRIX_MAX_GROUPS]],
            }
        )
    chains.sort(key=lambda c: (-c["count"], -len(c["groups"]), c["id"].casefold()))
    chains = chains[:_MATRIX_MAX_CHAINS]
    if chains:
        chains[0]["default"] = True
    return {"chains": chains, "items": items}


async def _aev_finished_exercises(
    base_url: str,
    headers: dict[str, str],
    client: httpx.AsyncClient,
    *,
    since_iso: str | None = None,
) -> tuple[list[dict[str, Any]], int | None]:
    """Finished simulations in the validation window (sampled) + total count.

    Windowed to ``since_iso`` (a start-date floor) so the aggregated expectation
    results match the OpenAEV home donuts' time filter. Returns ``(sims,
    sims_total)``; ``([], None)`` when the search fails. If the WINDOWED query
    errors (e.g. an older build that rejects the date filter) we retry WITHOUT the
    window so validation still gets data rather than nothing; a successful-but-
    empty window is kept as a real "no finished sims in the window".
    """
    resp, err = await _api(
        base_url, headers, "post", "/api/exercises/search", "list finished exercises",
        json=_search_body(size=_SIM_SAMPLE, filter_group=_aev_finished_filter(since_iso)),
        client=client,
    )
    if err and since_iso:
        resp, err = await _api(
            base_url, headers, "post", "/api/exercises/search", "list finished exercises",
            json=_search_body(size=_SIM_SAMPLE, filter_group=_aev_finished_filter(None)),
            client=client,
        )
    if err or resp is None:
        return [], None
    payload = resp.json() or {}
    return (payload.get("content") or []), payload.get("totalElements")


async def _collect_openaev(
    base_url: str, headers: dict[str, str], user_id: str, client: httpx.AsyncClient
) -> dict[str, dict]:
    """Gather per-stage OpenAEV facts for *user_id* (live, scoped).

    Every independent OpenAEV REST call is fanned out concurrently (bounded by
    ``_AEV_MAX_CONCURRENCY``) instead of run sequentially - the bulk of a live
    build's OpenAEV cost. The only step left sequential is the legacy
    per-exercise ATT&CK scan, used solely as a fallback on OpenAEV builds that
    lack the global coverage-matrix endpoint.
    """
    stages: dict[str, dict] = {}

    # Scoping - attack surface in scope. AGENT-AGNOSTIC by design: OpenAEV
    # legitimately runs agentless assets (network / email / web targets hit via
    # injectors, not endpoint agents), so endpoint-agent coverage is NOT a valid
    # "protection" signal and overstates the gap. We measure the breadth of the
    # attack surface instead: total managed endpoints (inventory), asset groups
    # (scope organisation), detection sources, and how many ATT&CK attack
    # *domains* have validation content - OpenAEV's own "security domain power".
    #
    # Fan every independent call out at once (bounded): scoping inventory,
    # prioritization totals, the finished-simulation sample, attack-pattern total,
    # active collectors, and the global ATT&CK coverage matrix.
    # Window the validation expectation results to the last ~3 months so the
    # donuts + success rates match the OpenAEV home dashboard's time filter.
    validation_since = (
        datetime.now(UTC) - timedelta(days=_AEV_VALIDATION_WINDOW_DAYS)
    ).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    (
        endpoints_total,
        asset_groups_total,
        security_platforms_total,
        domain_coverage,
        findings_total,
        findings_cve,
        scenarios_total,
        payloads_total,
        atomic_tests_total,
        finished_exercises,
        attack_patterns_total,
        collectors_active,
        matrix,
        kill_chain_phases,
    ) = await _gather_bounded(
        _AEV_MAX_CONCURRENCY,
        _aev_total(base_url, headers, "/api/endpoints/search", client=client),
        _aev_asset_groups_total(base_url, headers, client),
        _aev_total(base_url, headers, "/api/security_platforms/search", client=client),
        _aev_domain_coverage(base_url, headers, client),
        _aev_total(base_url, headers, "/api/findings/search", client=client),
        _aev_total(
            base_url, headers, "/api/findings/search",
            _search_body(size=1, filter_group=_AEV_CVE_FILTER), client=client,
        ),
        _aev_total(base_url, headers, "/api/scenarios/search", client=client),
        _aev_total(base_url, headers, "/api/payloads/search", client=client),
        _aev_total(base_url, headers, "/api/atomic-testings/search", client=client),
        _aev_finished_exercises(base_url, headers, client, since_iso=validation_since),
        _aev_total(base_url, headers, "/api/attack_patterns/search", client=client),
        _aev_collectors_active(base_url, headers, client),
        _aev_attack_coverage(base_url, headers, client),
        _aev_kill_chain_phases(base_url, headers, client),
    )
    domains_covered, domains_total, domains_breakdown = domain_coverage
    sims, sims_total = finished_exercises

    # Breadth of the attack surface actually IN SCOPE - the OpenAEV scoping
    # signal. Counts how many scope dimensions are populated: managed endpoints,
    # asset groups, detection sources (security platforms) and ATT&CK attack
    # domains with validation content. Deliberately DISTINCT from
    # ``attack_surface_pct`` (domain-content coverage only): a connected-but-
    # sparse OpenAEV - rich inventory but no BAS content mapped to any attack
    # domain yet - must credit the inventory in scope rather than tank Scoping to
    # 0. Each dimension counts only when its call succeeded (value is not None);
    # if every call failed the signal is omitted (None) so it cannot drag the
    # OpenCTI scope-completeness blend down.
    aev_scope_known = [
        v
        for v in (
            endpoints_total,
            asset_groups_total,
            security_platforms_total,
            domains_covered,
        )
        if v is not None
    ]
    stages["scoping"] = {
        "endpoints_total": endpoints_total,
        "asset_groups_total": asset_groups_total,
        "security_platforms_total": security_platforms_total,
        "attack_domains_covered": domains_covered,
        "attack_domains_total": domains_total,
        # Per-domain breakdown for the Scoping ring (viz only, not a score input).
        "attack_domains": domains_breakdown,
        "attack_surface_pct": (
            pct(domains_covered, domains_total)
            if domains_covered is not None and domains_total
            else None
        ),
        "scope_breadth_pct": (
            pct(sum(1 for v in aev_scope_known if v > 0), len(aev_scope_known))
            if aev_scope_known
            else None
        ),
    }

    # Prioritization (findings, secondary to OpenCTI)
    stages["prioritization"] = {
        "findings_total": findings_total,
        "findings_cve": findings_cve,
        "scenarios_total": scenarios_total,
        "payloads_total": payloads_total,
        "atomic_tests_total": atomic_tests_total,
    }

    # Validation - aggregate the sampled finished simulations' expectation buckets.
    type_buckets: dict[str, dict[str, int]] = {}
    for sim in sims:
        for ert in sim.get("exercise_global_score") or []:
            etype = ert.get("type")
            if not etype:
                continue
            bucket = type_buckets.setdefault(etype, {})
            for d in ert.get("distribution") or []:
                bid = d.get("id")
                if bid:
                    bucket[bid] = bucket.get(bid, 0) + (d.get("value") or 0)

    # Prefer the global ATT&CK coverage matrix (per-technique prevention/detection
    # across the latest finished simulations) for exercised techniques + validated
    # counts; fall back to scanning each simulation on older OpenAEV builds.
    exercised: set[str] = set()
    exercised_tcodes: set[str] = set()  # MITRE external IDs (T-codes), cross-product coverage
    techniques_validated: int | None = None
    techniques_with_detection: int | None = None
    # Global PREVENTION/DETECTION tallies from the very same Elasticsearch
    # aggregation that powers the OpenAEV home security-coverage matrix
    # (``*_success`` = SUCCESS docs, ``*_total`` = SUCCESS + FAILED docs). This is
    # the authoritative source for the validation success rates below.
    prev_success_sum = prev_total_sum = det_success_sum = det_total_sum = 0
    if matrix is not None:
        techniques_validated = 0
        techniques_with_detection = 0
        for row in matrix:
            apid = row.get("attack_pattern_id")
            if apid:
                exercised.add(apid)
            ext = row.get("attack_pattern_external_id")
            if ext:
                exercised_tcodes.add(str(ext).upper())
            prev_success_sum += _to_int(row.get("prevention_success"))
            prev_total_sum += _to_int(row.get("prevention_total"))
            det_success_sum += _to_int(row.get("detection_success"))
            det_total_sum += _to_int(row.get("detection_total"))
            det_ok = _to_int(row.get("detection_success")) > 0
            prev_ok = _to_int(row.get("prevention_success")) > 0
            if det_ok or prev_ok:
                techniques_validated += 1
            if det_ok:
                techniques_with_detection += 1
    else:
        for sim in sims[:_ATTACK_SCAN_SIMS]:
            eid = sim.get("exercise_id")
            if not eid:
                continue
            ap_resp, ap_err = await _api(
                base_url, headers, "get",
                f"/api/exercises/{eid}/injects/results-by-attack-patterns", "att&ck results",
                client=client,
            )
            if ap_err or ap_resp is None:
                continue
            for row in ap_resp.json() or []:
                ap = row.get("inject_attack_pattern")
                if isinstance(ap, dict):
                    ext = ap.get("attack_pattern_external_id")
                    if ext:
                        exercised_tcodes.add(str(ext).upper())
                    ap = ap.get("attack_pattern_id")
                if ap:
                    exercised.add(ap)

    prevention = type_buckets.get("PREVENTION", {})
    detection = type_buckets.get("DETECTION", {})
    # Validation success prefers the home-identical coverage matrix (populated
    # whenever the tenant has any prevention/detection result), and only falls
    # back to the finished-exercise ``exercise_global_score`` buckets on older
    # OpenAEV builds that lack the coverage endpoint. The buckets alone were
    # leaving the "Validation success" KPI on its grey SAMPLE placeholder on
    # tenants whose results are not carried by the sampled FINISHED simulations.
    # Validation success rates mirror the OpenAEV home DONUTS: the SUCCESS share
    # of the expectation-result distribution (Success / Success+Partial+Pending+
    # Failed) aggregated over the windowed finished simulations - the SAME
    # aggregation AND time window the home uses, so the card matches it. OpenAEV
    # is connected here (this collector only runs when registered), so an empty
    # distribution is a real 0 ("no results in the window"), not a blind spot.
    # (The all-time ATT&CK coverage matrix still drives the technique-coverage
    # facts below - exercised / validated techniques - which are cumulative.)
    prevention_success_pct = _aev_success_pct(prevention) or 0.0
    detection_success_pct = _aev_success_pct(detection) or 0.0
    # Open validation gaps (failed expectations) feed Discovery/Remediation; use
    # the matrix totals (SUCCESS+FAILED minus SUCCESS) when the per-exercise
    # buckets are empty so the gap count tracks the same source.
    bucket_failed = sum(b.get("FAILED", 0) + b.get("PARTIAL", 0) for b in (prevention, detection))
    failed_partial = bucket_failed or (
        (prev_total_sum - prev_success_sum) + (det_total_sum - det_success_sum)
    )
    # Completeness + content-hygiene guards on the success rates above. A
    # FINISHED simulation does NOT mean every expectation was answered by a real
    # source: OpenAEV closes the collection window on expiry, and pending rows
    # are excluded from its averages. Reporting the pending share and the drift
    # count keeps a flattering rate from being read as a complete one.
    expectation_pending_pct = _aev_pending_pct(type_buckets)
    # ``sims`` is the unpacked simulation list - NOT ``finished_exercises``, which
    # is the (sims, sims_total) tuple. Iterating the tuple called ``.get`` on the
    # list itself and crashed the whole OpenAEV collection.
    drifted_simulations = await _aev_expectations_drift(
        base_url,
        headers,
        client,
        [str(e["exercise_id"]) for e in sims if e.get("exercise_id")],
    )

    stages["validation"] = {
        "finished_simulations": sims_total,
        "expectation_buckets": type_buckets,
        "prevention_success_pct": prevention_success_pct,
        "detection_success_pct": detection_success_pct,
        # Both stay None when they could not be read (older build, no
        # permission, empty window): the posture API drops None-valued facts, so
        # an unknown signal is omitted instead of being reported as a confident
        # "0% pending" / "0 drifted", which would read as a clean bill of health.
        "expectation_pending_pct": expectation_pending_pct,
        "drifted_simulations": drifted_simulations,
        "vulnerability_success_pct": _aev_success_pct(type_buckets.get("VULNERABILITY", {})) or 0.0,
        "human_response_success_pct": _aev_success_pct(type_buckets.get("HUMAN_RESPONSE", {})) or 0.0,
        "techniques_exercised": len(exercised),
        "techniques_total": attack_patterns_total,
        "techniques_validated": techniques_validated,
        "techniques_with_detection": techniques_with_detection,
        "exercised_coverage_pct": pct(len(exercised), attack_patterns_total or 0) or 0.0,
        "security_platforms": security_platforms_total,
        "collectors_active": collectors_active,
        "failed_partial_expectations": failed_partial,
        # MITRE T-codes exercised (cross-product threat-informed coverage in Discovery).
        "exercised_tcodes": sorted(exercised_tcodes),
        # The Validation hero (viz only, not a score input): exercised techniques
        # by kill chain phase, for whichever frameworks the data carries.
        "outcome_matrix": _outcome_matrix(matrix, kill_chain_phases),
    }

    # Remediation (secondary to OpenCTI cases). Prevention efficacy (the share of
    # simulated attacks actually blocked) is a remediation/mitigation signal too,
    # so surface it here to feed the cyber Remediation score when OpenAEV is
    # connected.
    stages["remediation"] = {
        "open_findings": stages["prioritization"]["findings_total"],
        "failed_partial_expectations": failed_partial,
        "prevention_success_pct": prevention_success_pct,
        # The Mobilization page's mixed views (#11998): what OpenAEV still finds open,
        # the same two counts as above, nested so the history snapshot reads nothing new.
        "fix_backlog": _named_counts(
            ("open_findings", stages["prioritization"]["findings_total"]),
            ("failed_partial_expectations", failed_partial),
        ),
    }
    return stages


# ── Vertical dispatch ────────────────────────────────────────────────────────
# Which collectors feed each vertical. OpenAEV adds live BAS/phishing efficacy to
# cyber + fraud; FIMI is OpenCTI-native.
_VERTICAL_COLLECTORS: dict[str, list[tuple[str, Any]]] = {
    VERTICAL_CYBER: [("opencti", _collect_opencti_cyber), ("openaev", _collect_openaev)],
    VERTICAL_FIMI: [("opencti", _collect_opencti_fimi)],
    VERTICAL_FRAUD: [("opencti", _collect_opencti_fraud), ("openaev", _collect_openaev)],
}


def _funnel_link_filters(tailored: dict[str, Any]) -> dict[str, dict | None]:
    """The OpenCTI filter of each funnel layer, on the basis its count used (#11954).

    With a live ``relevant`` count the exploitable layer counted the tailored
    set, so it links with the tailored filter or with none (a stack too wide
    for a link, or matching no software); without one it counted every known
    vulnerability and links with the generic exploitable filter. The generic
    filter never stands in for a tailored count, nor the tailored one for a
    generic count.
    """
    if tailored.get("relevant") is None:
        return {"exploitable": _F_EXPLOITABLE}
    links = tailored.get("filters") or {}
    return {"relevant": links.get("relevant"), "exploitable": links.get("exploitable")}


def _attach_funnel(
    stage_facts: dict[str, dict[str, Any]],
    present: set[str],
    reachability: dict[str, int] | None = None,
    exposed: int | None = None,
) -> None:
    """Turn the collected funnel inputs into the prioritization ``funnel`` (#11546).

    The inputs never stay in the facts: without OpenCTI there is no funnel.
    ``reachability`` is the split of the profile assets: the Exposed layer
    shows it, and is live with ``exposed`` (:func:`_exposed_join`), else stays
    not available while the vulnerabilities the funnel counts could not be
    joined to an asset (never "not configured" when the profile holds assets).
    """
    prio = stage_facts.get("prioritization")
    if prio is None:
        return
    inputs = prio.pop("funnel_inputs", None)
    tailored = prio.pop("funnel_tailored", None) or {}
    if not inputs or "opencti" not in present:
        return
    funnel = build_funnel(
        known=inputs.get("known"),
        relevant=tailored.get("relevant"),
        exploitable_known=inputs.get("exploitable"),
        exploitable_relevant=tailored.get("exploitable"),
        asset_findings=prio.get("findings_cve") if "openaev" in present else None,
        filters=_funnel_link_filters(tailored),
        reachability=reachability,
        exposed=exposed,
    )
    if funnel is not None:
        prio["funnel"] = funnel


async def build_posture_for_user(
    db: AsyncSession, user_id: str, vertical: str = VERTICAL_CYBER
) -> tuple[dict[str, dict], set[str]]:
    """Gather merged per-stage CTEM facts for *user_id* in *vertical*.

    Returns ``(stage_facts, present_products)`` where ``present_products`` is the
    set of providers that returned data (so the API can mark the others as blind
    spots). Each provider is queried with the user's own JWT, so facts reflect
    only what that user can see.
    """
    vertical = normalize_vertical(vertical)
    stage_facts: dict[str, dict] = {}
    present: set[str] = set()

    logger.info("CTEM: collecting live %s posture for user %s", vertical, user_id)
    collectors = _VERTICAL_COLLECTORS[vertical]

    # Resolve each provider's creds first - DB-bound and therefore SEQUENTIAL (the
    # AsyncSession is not safe for concurrent use). _user_creds also releases the
    # pooled DB connection, so the network phase below never pins one. Then run the
    # providers' live collection CONCURRENTLY: OpenCTI and OpenAEV are independent
    # platforms, each on its own httpx client, so a cyber/fraud build costs about
    # max(OpenCTI, OpenAEV) instead of their sum.
    # The organization profile's tech stack (cyber): read in the same DB phase,
    # it narrows the KEV count to what the organization runs (#11213).
    stack = await _profile_tech_stack(db) if vertical == VERTICAL_CYBER else []
    asset_rows = await _profile_asset_rows(db) if vertical == VERTICAL_CYBER else None
    reachability = _profile_reachability(asset_rows)

    creds: dict[str, tuple[str, dict[str, str]] | None] = {}
    for provider in dict.fromkeys(p for p, _ in collectors):
        try:
            creds[provider] = await _user_creds(db, user_id, provider)
        except Exception as exc:  # a creds failure for one product never blocks the other
            logger.warning("CTEM creds error for %s (user %s): %s", provider, user_id, format_exception_message(exc))
            creds[provider] = None
    # A build runs for a user who did not ask for each query (a dashboard load,
    # a background refresh): an OpenCTI query their role cannot pass (PIRs,
    # playbooks...) is never sent, or OpenCTI logs "<user> tries an
    # unauthorized read". One capability probe per build, shared by the
    # collector and the tailoring.
    if creds.get("opencti"):
        octi_base, octi_headers = creds["opencti"]
        creds["opencti"] = (octi_base, attach_probe(octi_headers, octi_base))

    async def _run(provider: str, collector: Any) -> tuple[str, dict[str, dict] | None]:
        resolved = creds.get(provider)
        if not resolved:
            return provider, None
        base_url, headers = resolved
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                return provider, await collector(base_url, headers, user_id, client)
        except Exception as exc:  # one product failing never blocks the other
            logger.warning("CTEM collection error for %s (user %s): %s", provider, user_id, format_exception_message(exc))
            return provider, None

    async def _tailor() -> dict[str, Any] | None:
        resolved = creds.get("opencti")
        if not stack or not resolved:
            return None
        base_url, headers = resolved
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                return await _octi_kev_tailored(client, base_url, headers, stack, user_id)
        except Exception as exc:  # tailoring is best-effort: the generic count stays
            logger.warning("CTEM tailoring error (user %s): %s", user_id, format_exception_message(exc))
            return None

    *results, tailored = await asyncio.gather(
        *(_run(provider, collector) for provider, collector in collectors), _tailor()
    )
    for provider, result in results:
        if result is None:
            continue
        present.add(provider)
        for stage, facts in result.items():
            stage_facts.setdefault(stage, {}).update(facts)

    # Cross-product Discovery (cyber): threat-informed defense coverage = the
    # share of ATT&CK techniques used by tracked threats (OpenCTI) that are
    # actually exercised by OpenAEV simulations. The untested set is the gap.
    if vertical == VERTICAL_CYBER:
        disc = stage_facts.get("discovery", {})
        val = stage_facts.get("validation", {})
        threat = set(disc.get("threat_technique_tcodes") or [])
        # Compute whenever OpenAEV is connected (validation collected) and the
        # threat techniques are known - even with NOTHING exercised yet. That is a
        # real state (0 exercised, the whole threat set untested, 0% coverage), not
        # a blind spot, so surface real zeros instead of None (which the UI renders
        # as a greyed sample despite the data being real).
        if threat and "openaev" in present:
            exercised = set(val.get("exercised_tcodes") or [])
            covered_t = threat & exercised
            disc["covered_techniques"] = len(covered_t)
            disc["untested_techniques"] = len(threat - exercised)
            disc["exercised_techniques"] = len(exercised)
            disc["threat_informed_coverage_pct"] = pct(len(covered_t), len(threat))
        if tailored is not None and "opencti" in present:
            stage_facts.setdefault("prioritization", {}).update(tailored)
        exposed = await _exposed_join(creds, stage_facts, present, asset_rows, tailored, user_id)
        _attach_funnel(stage_facts, present, reachability, exposed)

    logger.info(
        "CTEM: %s posture collected for user %s — present=%s, stages_with_data=%d",
        vertical,
        user_id,
        sorted(present) or "none",
        sum(1 for f in stage_facts.values() if f),
    )
    return stage_facts, present
