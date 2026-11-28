# SPDX-License-Identifier: Apache-2.0

"""CTEM Cases: read the open issues a recompute groups (#11545, slice S3).

Design: ``dev-docs/ctem-cases.md`` section 4.1. Two sources, each read with
the acting user's own credentials (the posture's ``_user_creds``), so a case
only ever groups what that user can see:

- OpenCTI: the Vulnerabilities related by ``has`` to a Software observable
  matching the organization's Software entities (#1495: the synced and
  hand-typed software of the asset store, ``ctem_metrics_collector._organization_software``;
  the tailored KEV match, ``_stack_matches``), with the Course of Action that
  ``remediates`` or ``mitigates`` each one (rule R1). Without any software
  OpenCTI contributes nothing: no case from the whole vulnerability base.
- OpenAEV: the findings of type ``cve`` created within the validation window,
  with the assets they were found on (rule R3 joins them to OpenCTI by CVE).

Every read is bounded; an unreachable product contributes nothing and is named
in :attr:`SourceRead.missing`, never guessed.

The same read is a viewer's view of the shared cases (#11613,
``ctem_cases_view``): :func:`read_view` adds the members of the cases the
viewer can still read by id although they are no longer open (confirmed
closed); a member the viewer cannot read is in neither set, which is how the
source products decide what the viewer sees.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.utils import format_exception_message
from app.integrations.providers.openaev._common import Finding, _api, _search_body, parse_finding
from app.integrations.providers.opencti._common import _graphql
from app.integrations.providers.opencti.capabilities import attach_probe
from app.services import ctem_cases as rules
from app.services import ctem_cases_store as store
from app.services import ctem_fixed_version as fixed_versions
from app.services import ctem_product_identity as product_identity
from app.services.ctem_cases import VERSION_AFFECTED, VERSION_NOT_AFFECTED, VERSION_UNKNOWN
from app.services.ctem_metrics_collector import (
    _AEV_VALIDATION_WINDOW_DAYS,
    _fg,
    _organization_software,
    _read_stack_software,
    _stack_matches,
    _user_creds,
)
from app.services.ctem_verticals import VERTICAL_CYBER, normalize_vertical

logger = logging.getLogger(__name__)

#: Why a product contributed nothing to a read (#1852), per product in :attr:`SourceRead.reasons`.
REASON_REJECTED = "rejected"  # the product refused this user's token (401, 403)
REASON_TOO_LARGE = "too_large"  # the product could not answer the query (clause limit, database error)
REASON_TIMEOUT = "timeout"
REASON_NOT_CONNECTED = "not_connected"  # no credentials could be resolved for this user
REASON_UNAVAILABLE = "unavailable"  # anything else
#: Reasons that belong to one identity, never to the product: one user's failure is not an outage.
IDENTITY_REASONS = frozenset({REASON_REJECTED, REASON_NOT_CONNECTED})

#: How long the recompute's write phase waits for a row another writer holds (#1953).
WRITE_LOCK_TIMEOUT = "5s"

#: The integration fields ``integration_tools._get_token`` binds on the log context; a read
#: of another product must not carry them (#1852: the OpenCTI line named the OpenAEV integration).
_INTEGRATION_LOG_KEYS = (
    "integration_name",
    "integration_owner_user_id",
    "integration_auth_type",
    "integration_is_company_managed",
)

#: The error of the last failed source read in this task, set by the readers before they answer None.
_read_error: ContextVar[str | None] = ContextVar("ctem_cases_read_error", default=None)


def failure_reason(error: str | BaseException | None) -> str:
    """The reason kind of a failed read, from the provider's error text or the exception raised."""
    if isinstance(error, httpx.TimeoutException):
        return REASON_TIMEOUT
    text = (format_exception_message(error) if isinstance(error, BaseException) else (error or "")).lower()
    if not text:
        return REASON_UNAVAILABLE
    if any(m in text for m in ("auth_required", "401", "403", "forbidden", "unauthorized", "forbidden_access")):
        return REASON_REJECTED
    if any(m in text for m in ("too_many_clauses", "too many clauses", "database_error", "maxclausecount")):
        return REASON_TOO_LARGE
    if any(m in text for m in ("timed out", "timeout", "readtimeout", "connecttimeout")):
        return REASON_TIMEOUT
    return REASON_UNAVAILABLE


def _failed(err: str | None) -> None:
    _read_error.set(err)


def _bind_read_context(user_id: str, provider: str) -> None:
    """Name the reader and the product on every log line of this read, and drop the integration
    fields an earlier token resolution left on the context (#1852)."""
    structlog.contextvars.unbind_contextvars(*_INTEGRATION_LOG_KEYS)
    structlog.contextvars.bind_contextvars(ctem_read_user_id=str(user_id), ctem_read_provider=provider)


def _clear_read_context() -> None:
    structlog.contextvars.unbind_contextvars("ctem_read_user_id", "ctem_read_provider", *_INTEGRATION_LOG_KEYS)


#: Most vulnerability memberships (software x vulnerability) read from OpenCTI.
OCTI_ISSUE_CAP = 2000
#: Most open CVE findings read from OpenAEV. Since OpenAEV #9233 a finding is one per CVE and
#: asset, so this counts (CVE, asset) pairs open in the window: ten pages of :data:`_PAGE`.
AEV_FINDING_CAP = 5000
_PAGE = 500
#: Software ids per ``fromId`` list of the stack vulnerabilities read (#1811).
_STACK_IDS_CHUNK = 200

# The stack's Software observables with their version (CTEM-11, #946): the
# declared version of a stack entry is the Software node of that version.
_OCTI_STACK_SOFTWARE = """
query CtemCaseStackSoftware($filters: FilterGroup, $first: Int, $after: ID) {
  stixCyberObservables(types: ["Software"], filters: $filters, first: $first, after: $after) {
    pageInfo { hasNextPage endCursor }
    edges { node { id ... on Software { name vendor version cpe x_opencti_purl } } }
  }
}
"""

# The Software versions OpenCTI says remediate each vulnerability (CTEM-11, #946):
# the fixed version is read from OpenCTI, never computed from ranges here.
_OCTI_SOFTWARE_REMEDIATES = """
query CtemCaseSoftwareRemediates($to: [String], $first: Int) {
  stixCoreRelationships(toId: $to, relationship_type: ["remediates"], fromTypes: ["Software"], first: $first) {
    edges { node {
      from { ... on Software { id name vendor version cpe x_opencti_purl } }
      to { ... on Vulnerability { id } }
    } }
  }
}
"""

_OCTI_STACK_VULNS = """
query CtemCaseStackVulns($from: [String], $first: Int, $after: ID) {
  stixCoreRelationships(fromId: $from, relationship_type: ["has"], toTypes: ["Vulnerability"], first: $first, after: $after) {
    pageInfo { hasNextPage endCursor }
    edges { node {
      from { ... on StixCyberObservable { id } }
      to { ... on Vulnerability {
        id standard_id name created_at
        x_opencti_cvss_base_score x_opencti_epss_score x_opencti_cisa_kev
      } }
    } }
  }
}
"""

# The ``has`` relations of the stack to the vulnerabilities of one case (#1986): the case read's scope.
_OCTI_CASE_VULNS = """
query CtemCaseScopedVulns($from: [String], $to: [String], $first: Int, $after: ID) {
  stixCoreRelationships(fromId: $from, toId: $to, relationship_type: ["has"], toTypes: ["Vulnerability"], first: $first, after: $after) {
    pageInfo { hasNextPage endCursor }
    edges { node {
      from { ... on StixCyberObservable { id } }
      to { ... on Vulnerability {
        id standard_id name created_at
        x_opencti_cvss_base_score x_opencti_epss_score x_opencti_cisa_kev
      } }
    } }
  }
}
"""

_OCTI_REMEDIATIONS = """
query CtemCaseRemediations($to: [String], $first: Int) {
  stixCoreRelationships(toId: $to, relationship_type: ["remediates", "mitigates"], fromTypes: ["Course-Of-Action"], first: $first) {
    edges { node {
      from { ... on CourseOfAction { id standard_id name } }
      to { ... on Vulnerability { id } }
    } }
  }
}
"""

_OCTI_VULNS_BY_ID = """
query CtemCaseVulnsById($filters: FilterGroup, $first: Int) {
  vulnerabilities(filters: $filters, first: $first) {
    edges { node {
      id name created_at
      x_opencti_cvss_base_score x_opencti_epss_score x_opencti_cisa_kev
    } }
  }
}
"""

# The vulnerabilities a signal member names (#2041): looked up by CVE name, with the viewer's own read.
_OCTI_VULNS_BY_NAME = """
query CtemCaseSignalVulns($filters: FilterGroup, $first: Int) {
  vulnerabilities(filters: $filters, first: $first) {
    edges { node {
      id name
      x_opencti_cvss_base_score x_opencti_epss_score x_opencti_cisa_kev
    } }
  }
}
"""

#: Most CVE ids one signal enrichment looks up (#2041): past it the rest stays unenriched, never guessed.
SIGNAL_CVE_CAP = 500

#: Most member lookups by id one OpenAEV read makes; past it the read is incomplete and resolves
#: nothing. One finding per CVE and asset (OpenAEV #9233) makes more members than before.
AEV_LOOKUP_CAP = 1000
#: Most CVE searches one identity remap makes (members OpenAEV answers 404 for, OpenAEV #9233).
AEV_REMAP_CVE_CAP = 200
#: OpenAEV lookups by id in flight at once (#1986): one finding per call, so they go in parallel, bounded.
AEV_LOOKUP_CONCURRENCY = 8

#: The product names of the log lines.
_PRODUCT_LABELS = {"opencti": "OpenCTI", "openaev": "OpenAEV"}

_AEV_CVE_FINDINGS_FILTER = {
    "mode": "and",
    "filters": [{"key": "finding_type", "mode": "or", "operator": "eq", "values": ["cve"]}],
}

#: The server-side window of ``/api/findings/search``: ``finding_updated_at`` is the queryable
#: time (OpenAEV #9233 moves it on each occurrence). Any later write moves it too, so the rows it
#: keeps are a superset; :func:`_seen_since` then keeps a finding by its last detection,
#: ``finding_last_seen``, when OpenAEV answers it (OpenAEV #9846).
_AEV_FINDING_UPDATED_AT = "finding_updated_at"
#: Only the findings still open at source join a case (``finding_status`` is ``@Queryable``).
_AEV_OPEN_FINDING = {"key": "finding_status", "mode": "or", "operator": "eq", "values": ["OPEN"]}
#: The re-test statuses that close a member at source; anything else, a 404 included, never does.
_AEV_CLOSED_RETEST = frozenset({"not_reproduced", "resolved"})
#: The history line of a member whose finding OpenAEV's identity migration replaced by nothing.
RETIRED_BY_IDENTITY_MIGRATION = "Replaced by OpenAEV finding identity migration"


@dataclass
class OpenCtiRead:
    """The OpenCTI issues of the stack, and whether a cap stopped the read."""

    issues: list[rules.SourceIssue]
    complete: bool = True


@dataclass
class SourceRead:
    """The open issues read for one recompute, and the products that could not answer."""

    issues: list[rules.SourceIssue] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    #: Why each product of :attr:`missing` contributed nothing (``REASON_*``, #1852).
    reasons: dict[str, str] = field(default_factory=dict)
    no_stack: bool = False
    #: Members the reader can read by id that are closed at source (OpenAEV: re-test status
    #: NOT_REPRODUCED or RESOLVED only).
    closed: list[rules.SourceIssue] = field(default_factory=list)
    #: Members the reader can read by id that are not in the open read but not closed either
    #: (an OpenAEV finding still OPEN, last seen before the window): shown, never resolved.
    looked_up: list[rules.SourceIssue] = field(default_factory=list)
    #: OpenAEV members that answer 404 (OpenAEV #9233 rewrote every finding id).
    gone: list[str] = field(default_factory=list)
    #: ``openaev:<old id>`` -> ``openaev:<new id>``, or ``None`` to retire the member.
    remap: dict[str, str | None] = field(default_factory=dict)
    #: False when a read hit its cap: then nothing is resolved from it.
    complete: bool = True

    def fail(self, provider: str, reason: str) -> None:
        """Name *provider* among the missing products with *reason*."""
        if provider not in self.missing:
            self.missing.append(provider)
        self.reasons[provider] = reason


def _float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _when(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _installed_versions(entry: dict[str, Any]) -> list[str]:
    """The known installed versions of a stack entry: ``versions`` (#1783), else its one ``version``."""
    versions = [str(v).strip() for v in entry.get("versions") or () if str(v or "").strip()]
    single = (entry.get("version") or "").strip()
    if single and single not in versions:
        versions.insert(0, single)
    return list(dict.fromkeys(versions))


def _version_counts(entry: dict[str, Any]) -> dict[str, int]:
    """The assets the device inventory reports on each version of a stack entry (#2038), ``{}`` when not read."""
    counts = entry.get("version_counts")
    return {str(v): int(n) for v, n in counts.items() if str(v or "").strip()} if isinstance(counts, dict) else {}


def with_inventory_versions(
    stack: list[dict[str, Any]],
    inventory: Mapping[str, Mapping[str, int]],
    packages: Mapping[str, Mapping[str, int]] | None = None,
) -> list[dict[str, Any]]:
    """*stack* with every version the device inventory reports for each entry (pure, #2038).

    *inventory* is ``ctem_case_versions.inventory_versions``' answer, keyed by the entry's
    ``label``. An entry the inventory reports takes its versions (the entry's own known
    versions kept after them) and their asset counts (``version_counts``); its own
    ``version_source`` is kept, so a version the inventory does not report stays attributed
    to the Software entity or profile row it came from (``_version_facts`` reads
    ``inventory`` only for the versions in ``version_counts``). Its single ``version`` is kept
    only when exactly one version runs, never a guess. An entry the inventory does not report
    (a profile-only product) is unchanged.

    *inventory* leaves out the language package rows of the repositories (the MinIO SDK is not
    the MinIO server, #2375): its versions are what a CVE of a server or OS product reads.
    *packages*, the answer that keeps them, gives the entry a ``package_view`` when it differs:
    the versions a CVE whose every Software is a language package reads (:func:`_view`), the
    rule a case's running versions follow (#2223), so ``installed_version`` and
    ``running_versions`` agree.
    """
    out: list[dict[str, Any]] = []
    for entry in stack:
        label = entry.get("label") or ""
        view = _inventory_view(entry, inventory.get(label) or {})
        if packages is not None:
            package_counts = _counts(packages.get(label) or {})
            if package_counts != _counts(inventory.get(label) or {}):
                view = {**view, "package_view": _inventory_view(entry, package_counts)}
        out.append(view)
    return out


def _counts(found: Mapping[str, int]) -> dict[str, int]:
    return {str(v): int(n) for v, n in found.items() if str(v).strip()}


def _inventory_view(entry: dict[str, Any], found: Mapping[str, int]) -> dict[str, Any]:
    """*entry* with the inventory versions *found* (pure, #2038); unchanged when none."""
    counts = _counts(found)
    if not counts:
        return entry
    versions = list(dict.fromkeys([*counts, *_installed_versions(entry)]))
    return {
        **entry,
        "versions": versions,
        "version": versions[0] if len(versions) == 1 else "",
        "version_counts": counts,
    }


def _view(entry: dict[str, Any], language_package: bool) -> dict[str, Any]:
    """The versions of *entry* a CVE reads (pure, #2375): with the repositories' language package rows
    only when every Software of the CVE is a language package (:func:`with_inventory_versions`)."""
    package = entry.get("package_view")
    if language_package and isinstance(package, dict):
        return package
    return entry


def _remediating_node(node: dict[str, Any]) -> dict[str, Any]:
    """A remediating Software as the stack matches it (pure, #2038).

    A source may name the node ``<product> <version>`` (``Chrome 148.0.7778.168``) with or
    without a ``version`` field: the trailing word is read as the version when it parses as one
    and agrees with the field, and the name is matched without it. Any other node is unchanged.
    """
    name = " ".join(str(node.get("name") or "").split())
    head, _, tail = name.rpartition(" ")
    version = str(node.get("version") or "").strip()
    if not head or fixed_versions.parse_version(tail) is None:
        return node
    if version and not fixed_versions.same_version(version, tail.lstrip("vV")) and version != tail:
        return node
    return {**node, "name": head, "version": version or tail}


#: Seconds a matcher stack is shared by every viewer build and pod of an organization (#2232): the
#: stack is the organization's software and inventory, never the viewer's, so one build computes it.
MATCHER_STACK_TTL_SECONDS = 300
#: Seconds a build waits for the stack another pod is computing before it computes its own.
MATCHER_STACK_WAIT_SECONDS = 180.0
_MATCHER_STACK_POLL_SECONDS = 0.5
#: One computation of a stack key at a time on this worker.
_MATCHER_STACK_LOCKS: dict[str, asyncio.Lock] = {}


def _matcher_stack_key(organization_id: uuid.UUID, profile_version: int) -> str:
    # v2: the entries carry their language package view apart (#2375).
    return f"ctem:cases:matcher-stack:v2:{organization_id}:{profile_version}"


async def _stack_redis() -> Any | None:
    try:
        from app.core.redis import redis_client

        return await redis_client()
    except Exception:  # no Redis: each worker computes its own stack
        return None


async def _cached_stack(client: Any, key: str) -> list[dict[str, Any]] | None:
    try:
        raw = await client.get(key)
    except Exception:
        logger.info("CTEM cases: matcher stack cache unreadable", exc_info=True)
        return None
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, list) else None


async def _matcher_stack(db: AsyncSession) -> list[dict[str, Any]]:
    """The organization's matcher stack (:func:`_compute_matcher_stack`), computed once per
    organization and profile version and shared across viewers and pods through Redis (#2232).

    A view build after a case-opening drain read it once per viewer and per pod (150 s each on
    ctem.one). A build that finds another pod computing it (``SET NX`` lock) waits for its answer
    up to :data:`MATCHER_STACK_WAIT_SECONDS`, then computes its own. Without Redis, or with no
    organization profile, it is computed as before.
    """
    from app.services import organization_context as org_context

    try:
        row = await org_context.current_version(db)
    except Exception:
        row = None
    client = await _stack_redis() if row is not None else None
    if row is None or client is None:
        return await _compute_matcher_stack(db)
    key = _matcher_stack_key(row.organization_id, int(row.version))
    cached = await _cached_stack(client, key)
    if cached is not None:
        return cached
    local = _MATCHER_STACK_LOCKS.setdefault(key, asyncio.Lock())
    async with local:
        cached = await _cached_stack(client, key)
        if cached is not None:
            return cached
        lock_key = f"{key}:lock"
        try:
            owner = bool(await client.set(lock_key, "1", ex=int(MATCHER_STACK_WAIT_SECONDS), nx=True))
        except Exception:
            owner = True
        if not owner:
            deadline = time.monotonic() + MATCHER_STACK_WAIT_SECONDS
            while time.monotonic() < deadline:
                await asyncio.sleep(_MATCHER_STACK_POLL_SECONDS)
                cached = await _cached_stack(client, key)
                if cached is not None:
                    return cached
        try:
            stack = await _compute_matcher_stack(db)
            try:
                await client.set(key, json.dumps(stack), ex=MATCHER_STACK_TTL_SECONDS)
            except Exception:
                logger.info("CTEM cases: matcher stack not cached", exc_info=True)
            return stack
        finally:
            if owner:
                try:
                    await client.delete(lock_key)
                except Exception:
                    logger.info("CTEM cases: matcher stack lock not released", exc_info=True)
            _MATCHER_STACK_LOCKS.pop(key, None)


async def _compute_matcher_stack(db: AsyncSession) -> list[dict[str, Any]]:
    """The software the OpenCTI issues are read for, each with every version the device inventory
    reports (#2038): the same versions the case's ``running_versions`` shows, so each gets its
    own outcome. The inventory failing keeps the software's own versions."""
    started = time.perf_counter()
    stack = await _organization_software(db)
    software_ms = (time.perf_counter() - started) * 1000
    if not stack:
        return stack
    from app.services import ctem_case_versions as case_versions
    from app.services import organization_context as org_context

    try:
        row = await org_context.current_version(db)
        if row is None:
            return stack
        labels = [e["label"] for e in stack if e.get("label")]
        lap = time.perf_counter()
        inventory = await case_versions.inventory_versions(db, row.organization_id, labels)
        # The same read, cached: the answer a language package entry reads (#2375).
        packages = await case_versions.inventory_versions(db, row.organization_id, labels, language_packages=True)
        versions_ms = (time.perf_counter() - lap) * 1000
        lap = time.perf_counter()
        platforms = await case_versions.inventory_platforms(db, row.organization_id, labels)
        platforms_ms = (time.perf_counter() - lap) * 1000
    except Exception as exc:  # the inventory never breaks the issues read
        logger.info("CTEM cases: inventory versions unavailable: %s", format_exception_message(exc))
        return stack
    lap = time.perf_counter()
    merged = with_inventory_platforms(with_inventory_versions(stack, inventory, packages), platforms)
    # Where the products phase of a view build spends its time before any product call (#2232):
    # 110 s of a 111 s build on ctem.one with both product reads rejected in under a second.
    logger.info(
        "CTEM cases: matcher stack of %d software: software %.0f ms, inventory versions %.0f ms, "
        "inventory platforms %.0f ms, merge %.0f ms",
        len(stack),
        software_ms,
        versions_ms,
        platforms_ms,
        (time.perf_counter() - lap) * 1000,
    )
    return merged


def with_inventory_platforms(
    stack: list[dict[str, Any]], platforms: Mapping[str, Iterable[str]]
) -> list[dict[str, Any]]:
    """*stack* with the platforms of every asset running each entry, when all are known (pure, #2152).

    *platforms* is ``ctem_case_versions.inventory_platforms``' answer, keyed by the entry's
    ``label``; an entry it does not name is unchanged (no ``platforms``: nothing is evaluated).
    """
    out: list[dict[str, Any]] = []
    for entry in stack:
        named = tuple(platforms.get(entry.get("label") or "") or ())
        out.append({**entry, "platforms": list(named)} if named else entry)
    return out


def _highest(versions: list[str]) -> str | None:
    """The highest of *versions* that compare, else the first one (pure)."""
    if not versions:
        return None
    comparable = [v for v in versions if fixed_versions.parse_version(v) is not None]
    if not comparable:
        return versions[0]
    return max(comparable, key=lambda v: fixed_versions.parse_version(v) or ())


def opencti_issues(
    software: list[dict[str, Any]],
    stack: list[dict[str, Any]],
    relations: list[dict[str, Any]],
    remediations: list[dict[str, Any]],
    fixes: list[dict[str, Any]] | None = None,
) -> list[rules.SourceIssue]:
    """Build the OpenCTI issues from the raw nodes (pure: the reader feeds it).

    One issue per (vulnerability, matched stack product); the product and
    vendor are the stack entry's, so the key folds to what the profile lists.
    A vulnerability with a Course of Action takes it (R1, first by name).

    The version knowledge is OpenCTI's (CTEM-11, #946), never computed here:

    - the fixed version is the smallest version of a Software of the product
      that ``remediates`` the vulnerability (*fixes*), above the declared
      version; else the version a course of action names when it is a plain
      upgrade; else it stays unknown (``fixed version unknown``);
    - a remediating Software built for one platform (CPE ``target_sw``, or an
      operating system its name or version names) also gives the fix of that
      platform (``fixed_versions``, #1572);
    - with a declared version whose Software node exists, a vulnerability that
      node has no ``has`` to is not affected; when no Software of that version
      exists the issue is kept and says ``version not known to OpenCTI``;
    - every installed version is checked the same way (#1783): the issue
      carries one outcome per version (``version_outcomes``: ``affected``,
      ``not_affected`` or ``unknown``); it is not affected only when every
      installed version is, and the fix is searched above the highest version
      still exposed. No range is computed here: the outcome is OpenCTI's.
    """
    entry_by_label = {e["label"]: e for e in stack}
    product_of: dict[str, dict[str, Any]] = {}
    # The language ecosystem each stack Software is a package of, else ``None`` (#2223).
    ecosystem_of: dict[str, str | None] = {}
    # The platform each stack Software is built for (CPE target, else its name or version), #2152.
    platform_of: dict[str, str | None] = {}
    # The OpenCTI Software ids of each installed version, per stack entry.
    version_ids: dict[tuple[str, str], set[str]] = {}
    for node in software:
        label = _stack_matches(node, stack)
        if not label or not node.get("id"):
            continue
        entry = entry_by_label[label]
        product_of[node["id"]] = entry
        # A language package (pkg:pypi minio) is not the server product of the same name (#2223).
        ecosystem_of[node["id"]] = product_identity.software_ecosystem(node)
        platform_of[node["id"]] = fixed_versions.platform_of_software(
            node.get("name"), node.get("version"), node.get("cpe")
        )
        # Both views of the entry (#2375): without and with the repositories' language package rows.
        for view in (entry, _view(entry, True)):
            versions = _installed_versions(view)
            for version in versions:
                # A CPE match names the version only when one version runs (the CPE carries no version of its own).
                if fixed_versions.same_version(node.get("version"), version) or (
                    len(versions) == 1
                    and view.get("cpe")
                    and (node.get("cpe") or "").strip().lower() == view["cpe"].lower()
                ):
                    version_ids.setdefault((label, version), set()).add(node["id"])
    coa_of: dict[str, tuple[str, str]] = {}
    for rel in remediations:
        coa = rel.get("from") or {}
        vuln = (rel.get("to") or {}).get("id")
        coa_id = coa.get("standard_id") or coa.get("id")
        if not vuln or not coa_id:
            continue
        name = (coa.get("name") or "").strip()
        current = coa_of.get(vuln)
        if current is None or (name or coa_id) < (current[1] or current[0]):
            coa_of[vuln] = (coa_id, name)
    fixed_of: dict[tuple[str, str], list[str]] = {}
    # The platform each remediating Software is built for (#1572): CPE target, else its name or version.
    platform_fixes: dict[tuple[str, str], list[tuple[str, str | None]]] = {}
    # The remediating Software named per version, so the case says which node gave the fix (#928).
    fixed_name: dict[tuple[str, str, str], str] = {}
    for rel in fixes or []:
        node = rel.get("from") or {}
        vuln = (rel.get("to") or {}).get("id")
        label = _stack_matches(node, stack)
        if label is None:
            # ``Chrome 148.0.7778.168``: the product name carries its version (#2038).
            node = _remediating_node(node)
            label = _stack_matches(node, stack)
        version = (node.get("version") or "").strip()
        if vuln and label and version:
            fixed_of.setdefault((vuln, label), []).append(version)
            platform_fixes.setdefault((vuln, label), []).append(
                (version, fixed_versions.platform_of_software(node.get("name"), version, node.get("cpe")))
            )
            software = (node.get("name") or "").strip() or entry_by_label[label]["product"]
            fixed_name.setdefault((vuln, label, version), f"{software} {version}"[:300])
    holders: dict[tuple[str, str], set[str]] = {}
    vuln_of: dict[str, dict[str, Any]] = {}
    order: list[tuple[str, str]] = []
    for rel in relations:
        sw_id = (rel.get("from") or {}).get("id") or ""
        vuln = rel.get("to") or {}
        held = product_of.get(sw_id)
        if held is None or not vuln.get("id"):
            continue
        pair = (vuln["id"], held["label"])
        if pair not in holders:
            holders[pair] = set()
            order.append(pair)
            vuln_of[vuln["id"]] = vuln
        holders[pair].add(sw_id)
    out: list[rules.SourceIssue] = []
    for vuln_id, label in order:
        # Every Software the vulnerability names is a language package: the repository rows count (#2223, #2375).
        language_package = all(ecosystem_of.get(sw) for sw in holders[(vuln_id, label)])
        vuln, entry = vuln_of[vuln_id], _view(entry_by_label[label], language_package)
        name = (vuln.get("name") or "").strip() or vuln_id
        coa = coa_of.get(vuln_id)
        versions = _installed_versions(entry)
        installed = versions[0] if len(versions) == 1 else None
        outcomes: list[tuple[str, str]] = []
        for version in versions:
            ids = version_ids.get((label, version), set())
            if not ids:
                outcomes.append((version, VERSION_UNKNOWN))
            elif holders[(vuln_id, label)] & ids:
                outcomes.append((version, VERSION_AFFECTED))
            else:
                outcomes.append((version, VERSION_NOT_AFFECTED))
        facts = _version_facts(entry, outcomes, fixed_of.get((vuln_id, label), ()), fixed_name, vuln_id, label)
        not_affected: str | None = None
        notes: list[str] = []
        unknown = [v for v, o in outcomes if o == VERSION_UNKNOWN]
        if unknown:
            notes.append(f"{fixed_versions.VERSION_NOT_KNOWN}: {', '.join(unknown)}")
        elif outcomes and all(o == VERSION_NOT_AFFECTED for _v, o in outcomes):
            not_affected = (
                f"{fixed_versions.NOT_AFFECTED_PREFIX} {', '.join(versions)} has no 'has' relation to {name} in OpenCTI"
            )
        if not not_affected:
            not_affected = platform_not_affected(
                [platform_of.get(sw) for sw in sorted(holders[(vuln_id, label)])], entry.get("platforms")
            )
        # The fix must cover the highest version still exposed (affected, or unknown to OpenCTI).
        exposed = _highest([v for v, o in outcomes if o != VERSION_NOT_AFFECTED])
        fixed = fixed_versions.smallest_fix_above(fixed_of.get((vuln_id, label), ()), exposed)
        per_platform = fixed_versions.fixes_by_platform(platform_fixes.get((vuln_id, label), ()), exposed)
        fixed_by = fixed_name.get((vuln_id, label, fixed)) if fixed else None
        if fixed is None:
            fixed = fixed_versions.version_from_course_of_action(coa[1] if coa else None)
        if fixed is None and not not_affected:
            notes.append(fixed_versions.FIXED_VERSION_UNKNOWN)
        out.append(
            rules.SourceIssue(
                source="opencti",
                # The same CVE on two products is two keys, so two memberships.
                external_id=vuln_id,
                name=name[:300],
                cve=name if name.upper().startswith("CVE-") else None,
                product=entry["product"],
                vendor=entry["vendor"] or None,
                course_of_action_id=coa[0] if coa else None,
                course_of_action_name=coa[1] if coa else None,
                cvss=_float(vuln.get("x_opencti_cvss_base_score")),
                epss=_float(vuln.get("x_opencti_epss_score")),
                kev=bool(vuln.get("x_opencti_cisa_kev")),
                first_seen_at=_when(vuln.get("created_at")),
                fixed_version=fixed,
                installed_version=installed,
                not_affected=not_affected,
                version_note="; ".join(notes) or None,
                fixed_by=fixed_by,
                fixed_versions=tuple(sorted(per_platform.items())) if fixed and fixed_by else (),
                declared_version_known=any(o != VERSION_UNKNOWN for _v, o in outcomes) if outcomes else None,
                version_outcomes=facts,
                language_package=language_package,
            )
        )
    return out


def platform_not_affected(vulnerable: Iterable[str | None], running: Iterable[str] | None) -> str | None:
    """The not-affected reason when the CVE's Software and the product's assets share no platform (pure, #2152).

    *vulnerable* is the platform of each Software OpenCTI relates to the vulnerability
    (:func:`ctem_fixed_version.platform_of_software`); *running* the platforms of every asset
    running the product (``ctem_case_versions.inventory_platforms``). ``None`` (the member is kept)
    unless every vulnerable Software names a platform, every asset reports one, and none is shared:
    an unknown on either side is never read as not affected.
    """
    built = list(vulnerable)
    on = [p for p in running or () if str(p or "").strip()]
    if not built or not on or any(not p for p in built):
        return None
    built_keys = {str(p).casefold() for p in built if p}
    if built_keys & {p.casefold() for p in on}:
        return None
    only = ", ".join(sorted({str(p) for p in built if p}))
    return f"{fixed_versions.NOT_AFFECTED_PLATFORM_PREFIX} {only} only; running on {', '.join(sorted(set(on)))}"


def _version_facts(
    entry: dict[str, Any],
    outcomes: list[tuple[str, str]],
    fixes: Iterable[str],
    fixed_name: Mapping[tuple[str, str, str], str],
    vuln_id: str,
    label: str,
) -> tuple[rules.VersionFact, ...]:
    """The typed fact of each installed version (#2038): its outcome, where it was read, its
    asset count and the smallest remediating Software above it OpenCTI holds (pure)."""
    counts = _version_counts(entry)
    source = entry.get("version_source") or None
    fixes = list(fixes)
    out: list[rules.VersionFact] = []
    for version, outcome in outcomes:
        fixed = fixed_versions.smallest_fix_above(fixes, version) if outcome != VERSION_NOT_AFFECTED else None
        out.append(
            rules.VersionFact(
                version=version,
                outcome=outcome,
                source=(rules.VERSION_SOURCE_INVENTORY if version in counts else source) or None,
                asset_count=counts.get(version),
                fixed_version=fixed,
                fixed_source=rules.FIX_SOURCE_OPENCTI_SOFTWARE if fixed else None,
                fixed_by=fixed_name.get((vuln_id, label, fixed)) if fixed else None,
            )
        )
    return tuple(out)


def openaev_issues(findings: list[dict[str, Any]]) -> list[rules.SourceIssue]:
    """Build the OpenAEV issues from raw findings of type ``cve`` (pure), each parsed once."""
    out: list[rules.SourceIssue] = []
    seen: set[str] = set()
    for raw in findings:
        f = parse_finding(raw)
        if f is None or not f.value or f.id in seen:
            continue
        seen.add(f.id)
        out.append(
            rules.SourceIssue(
                source="openaev",
                external_id=f.id[:200],
                name=f.value[:300],
                cve=f.value if f.value.upper().startswith("CVE-") else None,
                assets=f.asset_labels,
                first_seen_at=_when(f.first_seen),
                **_finding_lifecycle(f),
            )
        )
    return out


#: OpenAEV finding statuses (CTEM-14) as the case reads them.
_FINDING_STATUSES = {"OPEN": "open", "NOT_REPRODUCED": "not_reproduced", "RESOLVED": "resolved"}


def _finding_lifecycle(f: Finding) -> dict[str, Any]:
    """The re-test status of a finding; a search row carries none (only ``GET /api/findings/{id}`` does)."""
    tested = _when(f.last_tested)
    return {
        "retest_status": _FINDING_STATUSES.get(f.status or ""),
        "last_tested_at": tested.isoformat() if tested else None,
        "retest_pending": bool(f.retest_inject_id),
    }


async def _read_opencti(
    client: httpx.AsyncClient,
    base_url: str,
    headers: dict[str, str],
    stack: list[dict[str, Any]],
) -> OpenCtiRead | None:
    """The open OpenCTI issues of *stack*, read with the caller's own *headers* (never a system token).

    The stack lookup and the ``fromId`` list go in batches under OpenCTI's
    clause limit (#1811); a cap reached marks the read incomplete, never failed.
    The time of each phase is logged (#2232), so a slow view build names its slow phase.
    """
    started = time.perf_counter()
    software, complete, err = await _read_stack_software(
        client, base_url, headers, _OCTI_STACK_SOFTWARE, stack, graphql=_graphql
    )
    if err:
        logger.info("CTEM cases: OpenCTI stack software unavailable: %s", err)
        _failed(err)
        return None
    ids = [n["id"] for n in software if n.get("id") and _stack_matches(n, stack)]
    if not ids:
        return OpenCtiRead([], complete)
    relations: list[dict[str, Any]] = []
    for start in range(0, len(ids), _STACK_IDS_CHUNK):
        after: str | None = None
        while len(relations) < OCTI_ISSUE_CAP:
            variables = {"from": ids[start : start + _STACK_IDS_CHUNK], "first": _PAGE, "after": after}
            data, err = await _graphql(client, base_url, headers, _OCTI_STACK_VULNS, variables)
            if err:
                logger.info("CTEM cases: OpenCTI stack vulnerabilities unavailable: %s", err)
                _failed(err)
                return None
            conn = (data or {}).get("stixCoreRelationships") or {}
            relations.extend((e or {}).get("node") or {} for e in conn.get("edges") or [])
            info = conn.get("pageInfo") or {}
            after = info.get("endCursor")
            if not info.get("hasNextPage") or not after:
                break
        if len(relations) >= OCTI_ISSUE_CAP:
            complete = False
            break
    relations = relations[:OCTI_ISSUE_CAP]
    read_at = time.perf_counter()
    remediations, fixes = await _remediations_of(client, base_url, headers, _vulnerability_ids(relations))
    built_at = time.perf_counter()
    # Pure CPU work over every relation (#2232): off the event loop, so the web worker keeps answering.
    issues = await asyncio.to_thread(opencti_issues, software, stack, relations, remediations, fixes)
    logger.info(
        "CTEM cases: OpenCTI read of %d software, %d relations, %d fixes: stack+relations %.0f ms, "
        "remediations %.0f ms, issues %.0f ms (%d issues)",
        len(software),
        len(relations),
        len(fixes),
        (read_at - started) * 1000,
        (built_at - read_at) * 1000,
        (time.perf_counter() - built_at) * 1000,
        len(issues),
    )
    return OpenCtiRead(issues, complete and len(issues) < OCTI_ISSUE_CAP)


def _vulnerability_ids(relations: list[dict[str, Any]]) -> list[str]:
    return sorted({str(vid) for r in relations if (vid := (r.get("to") or {}).get("id"))})


async def _remediations_of(
    client: httpx.AsyncClient, base_url: str, headers: dict[str, str], vuln_ids: list[str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The courses of action and the remediating Software of *vuln_ids*; a failed read is a known empty state."""
    remediations: list[dict[str, Any]] = []
    fixes: list[dict[str, Any]] = []
    for start in range(0, len(vuln_ids), _PAGE):
        batch = {"to": vuln_ids[start : start + _PAGE], "first": _PAGE * 2}
        # The courses of action and the remediating Software of a batch are independent reads (#1986).
        (data, err), (fix_data, fix_err) = await asyncio.gather(
            _graphql(client, base_url, headers, _OCTI_REMEDIATIONS, batch),
            _graphql(client, base_url, headers, _OCTI_SOFTWARE_REMEDIATES, batch),
        )
        if err:  # no course of action is a known state: R2 still answers
            logger.info("CTEM cases: OpenCTI courses of action unavailable: %s", err)
        else:
            remediations.extend(
                (e or {}).get("node") or {}
                for e in ((data or {}).get("stixCoreRelationships") or {}).get("edges") or []
            )
        if fix_err:  # no remediating Software is a known state: the fixed version stays unknown
            logger.info("CTEM cases: OpenCTI remediating software unavailable: %s", fix_err)
        else:
            fixes.extend(
                (e or {}).get("node") or {}
                for e in ((fix_data or {}).get("stixCoreRelationships") or {}).get("edges") or []
            )
    return remediations, fixes


async def _read_opencti_scoped(
    client: httpx.AsyncClient,
    base_url: str,
    headers: dict[str, str],
    stack: list[dict[str, Any]],
    vuln_ids: list[str],
) -> OpenCtiRead | None:
    """The open OpenCTI issues of *stack* among *vuln_ids* only (#1986): the case read's slice of :func:`_read_opencti`.

    The same stack lookup, ``has`` relations, courses of action and remediating
    Software, restricted to the case's vulnerabilities (``toId``), so each issue
    carries the version knowledge the full read gives it.
    """
    software, complete, err = await _read_stack_software(
        client, base_url, headers, _OCTI_STACK_SOFTWARE, stack, graphql=_graphql
    )
    if err:
        logger.info("CTEM cases: OpenCTI stack software unavailable: %s", err)
        _failed(err)
        return None
    ids = [n["id"] for n in software if n.get("id") and _stack_matches(n, stack)]
    if not ids or not vuln_ids:
        return OpenCtiRead([], complete)
    relations: list[dict[str, Any]] = []
    for start in range(0, len(ids), _STACK_IDS_CHUNK):
        after: str | None = None
        while True:
            variables = {"from": ids[start : start + _STACK_IDS_CHUNK], "to": vuln_ids, "first": _PAGE, "after": after}
            data, err = await _graphql(client, base_url, headers, _OCTI_CASE_VULNS, variables)
            if err:
                logger.info("CTEM cases: OpenCTI case vulnerabilities unavailable: %s", err)
                _failed(err)
                return None
            conn = (data or {}).get("stixCoreRelationships") or {}
            relations.extend((e or {}).get("node") or {} for e in conn.get("edges") or [])
            info = conn.get("pageInfo") or {}
            after = info.get("endCursor")
            if not info.get("hasNextPage") or not after:
                break
    remediations, fixes = await _remediations_of(client, base_url, headers, _vulnerability_ids(relations))
    issues = await asyncio.to_thread(opencti_issues, software, stack, relations, remediations, fixes)
    return OpenCtiRead(issues, complete)


async def _read_openaev(
    client: httpx.AsyncClient, base_url: str, headers: dict[str, str], now: datetime
) -> list[rules.SourceIssue] | None:
    """The CVE findings open at source and seen in the window (last occurrence, OpenAEV #9233)."""
    since = (now - timedelta(days=_AEV_VALIDATION_WINDOW_DAYS)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    window = {
        "mode": "and",
        "filters": [
            *_AEV_CVE_FINDINGS_FILTER["filters"],
            _AEV_OPEN_FINDING,
            {"key": _AEV_FINDING_UPDATED_AT, "mode": "and", "operator": "gte", "values": [since]},
        ],
    }
    findings: list[dict[str, Any]] = []
    page = 0
    while len(findings) < AEV_FINDING_CAP:
        resp, err = await _api(
            base_url,
            headers,
            "post",
            "/api/findings/search",
            "search CVE findings",
            json=_search_body(page=page, size=_PAGE, filter_group=window),
            client=client,
            retry_transient=True,
        )
        if err or resp is None:
            logger.info("CTEM cases: OpenAEV findings unavailable: %s", err)
            _failed(err)
            return None
        body = resp.json() or {}
        content = body.get("content") or []
        findings.extend(f for f in content if isinstance(f, dict))
        page += 1
        if not content or page >= int(body.get("totalPages") or 0):
            break
    since_at = now - timedelta(days=_AEV_VALIDATION_WINDOW_DAYS)
    return openaev_issues([f for f in findings[:AEV_FINDING_CAP] if _seen_since(f, since_at)])


def _seen_since(raw: dict[str, Any], since: datetime) -> bool:
    """Whether a search row was detected in the window (OpenAEV #9846).

    An OpenAEV that answers ``finding_last_seen`` is read on it: a row the server window kept only
    because a later write moved its update time is left out. An older OpenAEV answers no detection
    time, so its row stays (the server already filtered it on ``finding_updated_at``).
    """
    f = parse_finding(raw)
    if f is None or not f.seen_reported:
        return True
    seen = _when(f.last_seen)
    return seen is None or seen >= since


async def _read_opencti_by_id(
    client: httpx.AsyncClient, base_url: str, headers: dict[str, str], ids: list[str]
) -> list[rules.SourceIssue] | None:
    """The vulnerabilities among *ids* the reader can read (no product: they are closed members)."""
    out: list[rules.SourceIssue] = []
    for start in range(0, len(ids), _PAGE):
        chunk = ids[start : start + _PAGE]
        data, err = await _graphql(
            client,
            base_url,
            headers,
            _OCTI_VULNS_BY_ID,
            {"filters": _fg([{"key": ["id"], "values": chunk}]), "first": len(chunk)},
        )
        if err:
            logger.info("CTEM cases: OpenCTI lookup by id unavailable: %s", err)
            return None
        for edge in ((data or {}).get("vulnerabilities") or {}).get("edges") or []:
            node = (edge or {}).get("node") or {}
            if not node.get("id"):
                continue
            name = (node.get("name") or "").strip() or node["id"]
            out.append(
                rules.SourceIssue(
                    source="opencti",
                    external_id=node["id"],
                    name=name[:300],
                    cve=name if name.upper().startswith("CVE-") else None,
                    cvss=_float(node.get("x_opencti_cvss_base_score")),
                    epss=_float(node.get("x_opencti_epss_score")),
                    kev=bool(node.get("x_opencti_cisa_kev")),
                    first_seen_at=_when(node.get("created_at")),
                )
            )
    return out


async def _read_openaev_by_id(
    client: httpx.AsyncClient, base_url: str, headers: dict[str, str], ids: list[str]
) -> tuple[list[rules.SourceIssue], list[str]] | None:
    """``(the findings among *ids* the reader can read, the ids OpenAEV answers 404 for)``.

    A 403 is not visible and a 404 is gone (OpenAEV #9233 rewrote every finding id: the
    identity remap of :func:`recompute_for_user` takes them); neither is an outage. Each
    finding comes with its re-test status (``GET /api/findings/{id}`` carries it). One call
    per finding, at most :data:`AEV_LOOKUP_CONCURRENCY` in flight (#1986).
    """
    gate = asyncio.Semaphore(AEV_LOOKUP_CONCURRENCY)

    async def lookup(fid: str) -> tuple[Any, str | None]:
        async with gate:
            return await _api(
                base_url, headers, "get", f"/api/findings/{fid}", "get finding", client=client, retry_transient=True
            )

    found: list[dict[str, Any]] = []
    gone: list[str] = []
    asked = ids[:AEV_LOOKUP_CAP]
    for fid, (resp, err) in zip(asked, await asyncio.gather(*(lookup(fid) for fid in asked)), strict=True):
        if resp is not None and resp.status_code == 200:
            body = resp.json()
            if isinstance(body, dict):
                found.append(body)
            continue
        if err and "404" in err:
            gone.append(fid)
            continue
        if err and "403" not in err:
            logger.info("CTEM cases: OpenAEV lookup by id unavailable: %s", err)
            return None
    return openaev_issues(found), gone


#: The by-id readers of each product: closed members and the case read's lookup (#1986).
_BY_ID_READERS = {"opencti": _read_opencti_by_id, "openaev": _read_openaev_by_id}


async def _lookup_by_id(source: str, cred: tuple[str, dict[str, str]], ids: list[str]) -> Any:
    """What *source* reads among *ids*, ``None`` when the product fails.

    OpenCTI: the issues read. OpenAEV: ``(issues, ids answering 404)`` (OpenAEV #9233).
    """
    reader: Any = _BY_ID_READERS[source]
    base_url, headers = cred
    if source == "opencti":
        headers = attach_probe(headers, base_url)
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            return await reader(client, base_url, headers, sorted(ids))
    except Exception as exc:
        logger.warning("CTEM cases: %s lookup by id failed: %s", source, format_exception_message(exc))
        return None


async def release_before_network(db: Any) -> None:
    """Hand *db*'s connection back to the pool before a product read (#2232).

    A source read waits on OpenCTI and OpenAEV for seconds to minutes; the session
    that read the credentials and the stack would keep its pool connection checked
    out through it (the case tools of an agent run held one for 340-498 s during a
    drain). The session re-acquires a connection on its next statement.
    """
    if db is None or not callable(getattr(db, "in_transaction", None)):
        return
    from app.core.database import release_db_connection_before_loop

    await release_db_connection_before_loop(db)


async def _read_product(
    user_id: str, provider: str, read: Callable[[httpx.AsyncClient], Awaitable[Any]]
) -> tuple[Any, str | BaseException | None]:
    """One product's read with its own client: ``(found, error)``, *found* ``None`` on a failure.

    Run as a task of its own (#1986), so the read context it binds and the read
    error a reader sets stay in that task.
    """
    _bind_read_context(user_id, provider)
    _read_error.set(None)
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            found = await read(client)
        return found, _read_error.get()
    except Exception as exc:
        logger.warning(
            "CTEM cases: %s read failed as user %s: %s",
            _PRODUCT_LABELS.get(provider, provider),
            user_id,
            format_exception_message(exc),
        )
        return None, exc


async def read_open_issues(
    db: AsyncSession,
    user_id: str,
    vertical: str,
    *,
    now: datetime | None = None,
    members: list[str] | None = None,
    remap: bool = False,
) -> SourceRead:
    """The open issues *user_id* can see in *vertical* (cyber only; other verticals read nothing).

    With *members* (``source:external_id`` of the shared cases), the members
    that are not among the open issues are looked up by id with the same
    credentials: the ones closed at source are :attr:`SourceRead.closed`. With
    *remap*, the OpenAEV members that answer 404 are matched to their finding
    after OpenAEV's identity migration (:attr:`SourceRead.remap`).
    """
    now = now or datetime.now(UTC)
    out = SourceRead()
    if normalize_vertical(vertical) != VERTICAL_CYBER:
        return out
    stack = await _matcher_stack(db)
    out.no_stack = not stack
    creds: dict[str, tuple[str, dict[str, str]] | None] = {}
    try:
        for provider in ("opencti", "openaev"):
            _bind_read_context(user_id, provider)
            try:
                creds[provider] = await _user_creds(db, user_id, provider)
            except Exception as exc:  # one product failing never blocks the other
                logger.warning(
                    "CTEM cases: creds error for %s as user %s: %s", provider, user_id, format_exception_message(exc)
                )
                creds[provider] = None
        octi = creds.get("opencti")
        aev = creds.get("openaev")
        # The two products are read at once (#1986), each in its own task: the read context
        # and the read error of one never reach the other.
        reads: dict[str, Awaitable[tuple[Any, str | BaseException | None]]] = {}
        if octi and stack:
            octi_url, octi_headers = octi
            reads["opencti"] = _read_product(
                user_id,
                "opencti",
                lambda client: _read_opencti(client, octi_url, attach_probe(octi_headers, octi_url), stack),
            )
        if aev:
            aev_url, aev_headers = aev
            reads["openaev"] = _read_product(
                user_id, "openaev", lambda client: _read_openaev(client, aev_url, aev_headers, now)
            )
        await release_before_network(db)
        answers = dict(zip(reads, await asyncio.gather(*reads.values()), strict=True))
        if "opencti" in answers:
            found, error = answers["opencti"]
            if found is None:
                out.fail("opencti", failure_reason(error))
                logger.info("CTEM cases: OpenCTI missing for user %s: %s", user_id, out.reasons["opencti"])
            else:
                out.issues.extend(found.issues)
                out.complete = out.complete and found.complete
        elif not octi:
            out.fail("opencti", REASON_NOT_CONNECTED)
            logger.info("CTEM cases: OpenCTI missing for user %s: %s", user_id, REASON_NOT_CONNECTED)
        if "openaev" in answers:
            found_aev, error = answers["openaev"]
            if found_aev is None:
                out.fail("openaev", failure_reason(error))
                logger.info("CTEM cases: OpenAEV missing for user %s: %s", user_id, out.reasons["openaev"])
            else:
                out.issues.extend(found_aev)
                out.complete = out.complete and len(found_aev) < AEV_FINDING_CAP
        else:
            out.fail("openaev", REASON_NOT_CONNECTED)
            logger.info("CTEM cases: OpenAEV missing for user %s: %s", user_id, REASON_NOT_CONNECTED)
        if members:
            await _read_closed_members(out, creds, members)
        if remap and "openaev" not in out.missing:
            await _remap_gone_openaev(db, out, creds.get("openaev"))
    finally:
        _clear_read_context()
    return out


async def _read_closed_members(
    out: SourceRead, creds: dict[str, tuple[str, dict[str, str]] | None], members: list[str]
) -> None:
    """Fill :attr:`SourceRead.closed` with the members the reader reads by id but no longer sees open."""
    open_ids = {i.member_id for i in out.issues}
    wanted: dict[str, list[str]] = {}
    for member_id in members:
        source, _, external_id = member_id.partition(":")
        if member_id not in open_ids and source not in out.missing and creds.get(source):
            wanted.setdefault(source, []).append(external_id)
    # The products are looked up at once (#1986).
    lookups = {s: cred for s in wanted if s in _BY_ID_READERS and (cred := creds.get(s)) is not None}
    answers = await asyncio.gather(*(_lookup_by_id(s, cred, wanted[s]) for s, cred in lookups.items()))
    for source, found in zip(lookups, answers, strict=True):
        ids = wanted[source]
        if found is None:
            out.complete = False
            continue
        if source == "openaev":
            if len(ids) > AEV_LOOKUP_CAP:
                out.complete = False
            issues, gone = found
            out.gone.extend(f"openaev:{fid}" for fid in gone)
            # Closed at source only on OpenAEV's own word (NOT_REPRODUCED or RESOLVED): an OPEN finding
            # last seen before the window stays open and never resolves its case.
            for issue in issues:
                (out.closed if issue.retest_status in _AEV_CLOSED_RETEST else out.looked_up).append(issue)
            continue
        out.closed.extend(found)


async def _remap_gone_openaev(db: AsyncSession, out: SourceRead, cred: tuple[str, dict[str, str]] | None) -> None:
    """Fill :attr:`SourceRead.remap` for the OpenAEV members that answer 404 (OpenAEV #9233).

    OpenAEV's identity migration gave every finding a new id, one per CVE and asset. The CVE
    and the assets of a gone member are the ones its source signal recorded; the new finding
    is the one with the same CVE found on the same asset (or with no asset for a member that
    had none). No such finding: the member is retired (``None``). A CVE search that fails
    leaves its members out of the remap, so an outage never retires anything.
    """
    if not out.gone or cred is None:
        return
    from sqlalchemy import select

    from app.models.ctem_signal import CtemSignal

    old_ids = [m.partition(":")[2] for m in out.gone]
    rows = (
        await db.execute(
            select(CtemSignal.source_external_id, CtemSignal.evidence).where(
                CtemSignal.source_provider == "openaev", CtemSignal.source_external_id.in_(old_ids)
            )
        )
    ).all()
    known: dict[str, tuple[str, tuple[str, ...]]] = {}
    for external_id, evidence in rows:
        evidence = evidence or {}
        cve = str(evidence.get("cve") or "").strip()
        if external_id and cve and external_id not in known:
            known[external_id] = (cve, tuple(str(a) for a in evidence.get("assets") or []))
    base_url, headers = cred
    cves = sorted({cve for cve, _ in known.values()})[:AEV_REMAP_CVE_CAP]
    by_cve: dict[str, list[Finding]] = {}
    async with httpx.AsyncClient(timeout=30) as client:
        for cve in cves:
            found = await _search_cve_findings(client, base_url, headers, cve)
            if found is not None:
                by_cve[cve] = found
    for old in old_ids:
        member = f"openaev:{old}"
        if old not in known:
            # No signal names its CVE: nothing to match it with, so it is retired.
            out.remap[member] = None
            continue
        cve, assets = known[old]
        if cve not in by_cve:
            continue  # the search failed or was capped: decided at a later read
        out.remap[member] = _identity_match(by_cve[cve], assets)


def _identity_match(candidates: list[Finding], assets: tuple[str, ...]) -> str | None:
    """``openaev:<id>`` of the finding found on the first of *assets*, or with no asset for none."""
    if not assets:
        match = next((f for f in candidates if not f.assets), None)
        return f"openaev:{match.id}" if match else None
    for label in assets:
        for f in candidates:
            if label in f.asset_labels or label in f.asset_ids:
                return f"openaev:{f.id}"
    return None


async def _search_cve_findings(
    client: httpx.AsyncClient, base_url: str, headers: dict[str, str], cve: str
) -> list[Finding] | None:
    """Every CVE finding of value *cve*, whatever its status; ``None`` on an error or past a page."""
    group = {
        "mode": "and",
        "filters": [
            *_AEV_CVE_FINDINGS_FILTER["filters"],
            {"key": "finding_value", "mode": "or", "operator": "eq", "values": [cve]},
        ],
    }
    resp, err = await _api(
        base_url,
        headers,
        "post",
        "/api/findings/search",
        "search CVE findings",
        json=_search_body(page=0, size=_PAGE, filter_group=group),
        client=client,
        retry_transient=True,
    )
    if err or resp is None:
        logger.info("CTEM cases: OpenAEV identity remap search unavailable: %s", err)
        return None
    body = resp.json() or {}
    if int(body.get("totalPages") or 0) > 1:
        return None
    parsed = (parse_finding(f) for f in body.get("content") or [])
    return [f for f in parsed if f is not None and f.value.upper() == cve.upper()]


async def read_view(db: AsyncSession, user_id: str, vertical: str) -> Any:
    """One viewer's view of the shared cases (``ctem_cases_view.ViewerView``) from their own read."""
    from app.services.ctem_cases_view import ViewerView
    from app.services.organizations import get_primary

    vertical = normalize_vertical(vertical)
    phases: dict[str, float] = {}
    started = time.perf_counter()

    def lap(name: str) -> None:
        nonlocal started
        now = time.perf_counter()
        phases[name] = (now - started) * 1000
        started = now

    primary = await get_primary(db)
    members = await store.member_ids(db, organization_id=primary.id, vertical=vertical) if primary else []
    lap("members")
    read = await read_open_issues(db, user_id, vertical, members=members)
    lap("products")
    view = ViewerView(missing=list(read.missing), reasons=dict(read.reasons), complete=read.complete)
    # The members this read judged (#2113): a case opened after it is unjudged, never hidden.
    view.judged, view.built_at = set(members), datetime.now(UTC)
    view.opencrq = await _reads_opencrq(db, user_id)
    view.opencti = await _reads_opencti(db, user_id)
    view.openaev = await _reads_openaev(db, user_id)
    lap("access")
    closed, opened = [*read.closed, *read.looked_up], list(read.issues)
    if primary is not None:
        # #2563: a product member reaches the primary assets its software or its assets reach.
        reached = await _reach_primary_assets(db, primary.id, [*closed, *opened])
        closed, opened = reached[: len(closed)], reached[len(closed) :]
    lap("primary assets")
    for issue in closed:
        view.issues[issue.member_id] = issue
    for issue in opened:
        view.issues[issue.member_id] = issue
        view.open_ids.add(issue.member_id)
    if primary is not None:
        for issue in await _signal_members(db, organization_id=primary.id, user_id=user_id):
            view.issues[issue.member_id] = issue
            view.open_ids.add(issue.member_id)
    lap("signals")
    # Where a slow view build spends its time (#2232): the product reads alone were 5-7 s of 60-616 s.
    logger.info(
        "CTEM cases: view read of %s: %s (%d members)",
        user_id,
        ", ".join(f"{name} {ms:.0f} ms" for name, ms in phases.items()),
        len(members),
    )
    return view


async def read_case_view(db: AsyncSession, user_id: str, vertical: str, members: list[str]) -> Any:
    """One viewer's view of ONE case's *members* (``source:external_id``), from their own read (#1986).

    The case read's path when no view of the viewer is cached: the same
    credentials and the same rules as :func:`read_view`, scoped to the case.
    OpenCTI reads the stack's ``has`` relations to the case's vulnerabilities
    only; what is not open is looked up by id (closed); OpenAEV findings are
    looked up by id, bounded. A member the viewer cannot read stays out of the
    view (Restricted), a product that fails is ``missing``. The view is never
    cached: it judges only these members.
    """
    from app.services.ctem_cases_view import ViewerView
    from app.services.organizations import get_primary

    vertical = normalize_vertical(vertical)
    out = SourceRead()
    wanted: dict[str, list[str]] = {}
    for member_id in members:
        member_source, _, external_id = member_id.partition(":")
        if external_id:
            wanted.setdefault(member_source, []).append(external_id)
    view = ViewerView()
    if vertical == VERTICAL_CYBER and (wanted.get("opencti") or wanted.get("openaev")):
        await _read_case_members(db, user_id, out, wanted)
    view.missing, view.reasons, view.complete = list(out.missing), dict(out.reasons), out.complete
    view.judged, view.built_at = set(members), datetime.now(UTC)
    # One session: the two credential checks run one after the other, never concurrently.
    view.opencrq = await _reads_opencrq(db, user_id)
    view.opencti = await _reads_opencti(db, user_id)
    view.openaev = await _reads_openaev(db, user_id)
    closed, opened = [*out.closed, *out.looked_up], list(out.issues)
    # #2563: the same primary assets as the list row the viewer's view gives this case.
    reached = await _reach_primary_assets(db, None, [*closed, *opened])
    closed, opened = reached[: len(closed)], reached[len(closed) :]
    for issue in closed:
        view.issues[issue.member_id] = issue
    for issue in opened:
        view.issues[issue.member_id] = issue
        view.open_ids.add(issue.member_id)
    signal_ids = wanted.get("signal") or []
    primary = await get_primary(db) if signal_ids else None
    if primary is not None:
        for issue in await _signal_members(db, organization_id=primary.id, user_id=user_id, ids=signal_ids):
            view.issues[issue.member_id] = issue
            view.open_ids.add(issue.member_id)
    return view


def with_primary_assets(
    issues: Iterable[rules.SourceIssue], index: Any, names: Mapping[str, str]
) -> list[rules.SourceIssue]:
    """*issues* with the primary assets each product member reaches through the inventory links (pure, #2563).

    An OpenCTI member reaches them through the product it runs, an OpenAEV member
    through the assets it was found on. A signal member keeps the primary assets
    its gate decision recorded. One reach per distinct (software, assets) pair:
    the members of one product share it.
    """
    from app.services import ctem_primary_assets as pa

    out: list[rules.SourceIssue] = []
    reached: dict[tuple[tuple[str, ...], tuple[str, ...]], tuple[tuple[str, ...], tuple[str, ...]]] = {}
    for issue in issues:
        if issue.source == "signal" or not getattr(index, "reachers", None):
            out.append(issue)
            continue
        software = tuple(s for s in (issue.product,) if s)
        keys = tuple(sorted({pa.primary_asset_key(a) for a in issue.assets if pa.primary_asset_key(a)}))
        if not software and not keys:
            out.append(issue)
            continue
        hit = reached.get((software, keys))
        if hit is None:
            hit = pa.member_primary_assets(index, dict(names), software=list(software), keys=list(keys))
            reached[(software, keys)] = hit
        found, inferred = hit
        out.append(replace(issue, primary_assets=found, inferred_primary_assets=inferred) if found else issue)
    return out


async def _primary_asset_reach(db: AsyncSession, organization_id: uuid.UUID | None) -> tuple[Any, dict[str, str]]:
    """The inventory link index and the profile's primary asset names by link key (#2563).

    *organization_id* ``None`` reads the primary organization. An unreadable index
    or profile reaches nothing: the members stay as they are, never a failure.
    """
    from app.services import ctem_primary_assets as pa
    from app.services import organization_context as org_context
    from app.services.organizations import get_primary

    try:
        if organization_id is None:
            primary = await get_primary(db)
            if primary is None:
                return pa.PrimaryAssetIndex(), {}
            organization_id = primary.id
        async with db.begin_nested():
            index = await pa.load_index(db, organization_id)
            row = await org_context.current_version(db, organization_id)
        profile = org_context.parse_profile(row.profile if row else {})
    except Exception as exc:  # the case list never fails on the inventory links
        logger.info("CTEM cases: primary asset links unread: %s", format_exception_message(exc))
        return pa.PrimaryAssetIndex(), {}
    return index, {pa.primary_asset_key(j.name): j.name for j in profile.primary_assets}


async def _reach_primary_assets(
    db: AsyncSession, organization_id: uuid.UUID | None, issues: list[rules.SourceIssue]
) -> list[rules.SourceIssue]:
    """*issues* through :func:`with_primary_assets`, the index read once and the matching off the event loop."""
    if not any(i.source != "signal" for i in issues):
        return issues
    index, names = await _primary_asset_reach(db, organization_id)
    if not index.reachers:
        return issues
    return await asyncio.to_thread(with_primary_assets, issues, index, names)


async def _read_case_members(db: AsyncSession, user_id: str, out: SourceRead, wanted: dict[str, list[str]]) -> None:
    """Fill *out* with the product members of one case the viewer reads (open and closed)."""
    creds: dict[str, tuple[str, dict[str, str]] | None] = {}
    try:
        for provider in ("opencti", "openaev"):
            if not wanted.get(provider):
                continue
            _bind_read_context(user_id, provider)
            try:
                creds[provider] = await _user_creds(db, user_id, provider)
            except Exception as exc:  # one product failing never blocks the other
                logger.warning(
                    "CTEM cases: creds error for %s as user %s: %s", provider, user_id, format_exception_message(exc)
                )
                creds[provider] = None
            if creds[provider] is None:
                out.fail(provider, REASON_NOT_CONNECTED)
        octi = creds.get("opencti")
        stack = await _matcher_stack(db) if octi else []
        await release_before_network(db)
        if octi and stack:
            octi_url, octi_headers = octi
            vuln_ids = sorted(set(wanted["opencti"]))
            found, error = await _read_product(
                user_id,
                "opencti",
                lambda client: _read_opencti_scoped(
                    client, octi_url, attach_probe(octi_headers, octi_url), stack, vuln_ids
                ),
            )
            if found is None:
                out.fail("opencti", failure_reason(error))
            else:
                out.issues.extend(found.issues)
                out.complete = out.complete and found.complete
        # The members not open (every OpenAEV member, the OpenCTI ones off the stack) are looked up by id.
        await _read_closed_members(
            out, creds, [f"{s}:{i}" for s in ("opencti", "openaev") for i in wanted.get(s) or []]
        )
    finally:
        _clear_read_context()


async def _reads_opencrq(db: AsyncSession, user_id: str) -> bool:
    """Whether *user_id* reads OpenCRQ with their own credentials (#936): the gate on the
    OpenCRQ asset names and scenario ids of a case's exploitation reading."""
    try:
        return await _user_creds(db, user_id, "opencrq") is not None
    except Exception as exc:  # unreadable is not readable: the names stay restricted
        logger.info("CTEM cases: OpenCRQ access unreadable for %s: %s", user_id, format_exception_message(exc))
        return False


async def _reads_opencti(db: AsyncSession, user_id: str) -> bool:
    """Whether *user_id* reads OpenCTI with their own credentials (CTEM-16-UI, #1115): the
    gate on the Grouping name, ids and link of a case's threat package."""
    try:
        return await _user_creds(db, user_id, "opencti") is not None
    except Exception as exc:  # unreadable is not readable: the package stays restricted
        logger.info("CTEM cases: OpenCTI access unreadable for %s: %s", user_id, format_exception_message(exc))
        return False


async def _reads_openaev(db: AsyncSession, user_id: str) -> bool:
    """Whether *user_id* reads OpenAEV with their own credentials (#2468): the gate on the
    name, id and link of a case's validation scenario."""
    try:
        return await _user_creds(db, user_id, "openaev") is not None
    except Exception as exc:  # unreadable is not readable: the scenario stays restricted
        logger.info("CTEM cases: OpenAEV access unreadable for %s: %s", user_id, format_exception_message(exc))
        return False


async def _signal_members(
    db: AsyncSession, *, organization_id: uuid.UUID, user_id: str, ids: list[str] | None = None
) -> list[rules.SourceIssue]:
    """The signals attached to cases that *user_id* may read (#857), as case members.

    The signal API's own rule: an administrator or a CTEM view or decide right
    (``services/ctem_access.py``) reads every signal of the organization, anyone
    else the signals recorded for them. A case opened from
    a signal the viewer may not read stays absent, as a source issue they cannot
    see does.
    """
    from sqlalchemy import String, cast, select

    from app.models.ctem_case import CtemCaseIssue
    from app.models.ctem_signal import CtemSignal
    from app.models.user import User

    try:
        viewer = await db.get(User, uuid.UUID(str(user_id)))
    except (ValueError, TypeError):
        viewer = None
    statement = (
        select(CtemSignal)
        .join(
            CtemCaseIssue,
            (CtemCaseIssue.source == "signal") & (CtemCaseIssue.external_id == cast(CtemSignal.id, String)),
        )
        .where(CtemSignal.organization_id == organization_id)
    )
    from app.services import ctem_access

    whole = bool(getattr(viewer, "is_admin", False)) or (
        viewer is not None and await ctem_access.group_right(db, user_id) is not None
    )
    if not whole:
        statement = statement.where(CtemSignal.recorded_by == str(user_id))
    if ids is not None:  # one case's signals (#1986)
        statement = statement.where(cast(CtemSignal.id, String).in_(ids))
    rows = (await db.execute(statement)).scalars().all()
    from app.services.ctem_primary_assets import decision_assets, decision_primary_assets, decision_scope_kinds
    from app.services.ctem_scope_gate import signal_cve_ids

    # #2396: each primary asset the gate decision reached, with its link basis (proven or inferred).
    reached = {s.id: decision_primary_assets(s.decision or {}) for s in rows}
    # #2635: the inventory assets the gate matched, read from the decision at each view build, so the
    # cases opened before this change count them too (no backfill, no purge).
    matched = {s.id: decision_assets(s.decision or {}) for s in rows}
    # The CVE ids each signal names: enriched from OpenCTI like an OpenCTI issue (#2041).
    cves = {
        f"signal:{s.id}": found
        for s in rows
        if (found := signal_cve_ids(s.subject_ref, s.subject_label, s.evidence or {}))
    }
    issues = [
        rules.SourceIssue(
            source="signal",
            external_id=str(s.id),
            name=(s.subject_label or s.subject_ref or s.trigger_path)[:300],
            assets=tuple(
                dict.fromkeys(
                    [*((s.subject_ref,) if s.subject_kind == "asset" and s.subject_ref else ()), *matched[s.id]]
                )
            ),
            primary_assets=tuple(j["name"] for j in reached[s.id]),
            inferred_primary_assets=tuple(j["name"] for j in reached[s.id] if j["basis"] == "inferred"),
            scope_kinds=tuple(decision_scope_kinds(s.decision or {})),
            first_seen_at=s.first_seen_at,
            # #2755: the running version the signal itself reports judges its CVE, never "not reported".
            installed_version=signal_running_version(s.evidence),
        )
        for s in rows
    ]
    return await _enrich_signals(db, user_id, issues, cves) if cves else issues


def signal_running_version(evidence: Any) -> str | None:
    """The running version(s) a signal's evidence reports, joined, else ``None`` (#2755, pure)."""
    raw = evidence.get("running_version") if isinstance(evidence, dict) else None
    values = raw if isinstance(raw, list) else [raw]
    found = list(dict.fromkeys(str(v).strip() for v in values if isinstance(v, (str, int, float)) and str(v).strip()))
    return ", ".join(found)[:200] or None


def enrich_signal_issues(
    issues: list[rules.SourceIssue],
    cves: dict[str, list[str]],
    vulns: list[dict[str, Any]],
    remediations: list[dict[str, Any]] | None = None,
) -> list[rules.SourceIssue]:
    """The signal members with the facts OpenCTI holds on the CVEs they name (pure, #2041).

    *cves* maps a member id to the CVE ids its signal names; *vulns* are the
    OpenCTI Vulnerability nodes read by name, *remediations* their courses of
    action. A member takes its first CVE, KEV when any of its CVEs is KEV (each
    KEV CVE kept in ``kev_cves`` for the case's KEV count), the
    highest CVSS and EPSS, and the version a course of action names when it is
    a plain upgrade. A CVE OpenCTI does not know, or that was not read (no
    OpenCTI access for the reader, OpenCTI failed: *vulns* empty), leaves those
    facts unknown: kept in ``unknown_cves`` and ``kev`` is ``None`` unless another
    of its CVEs is KEV (#2225), never "not KEV"; the member is kept either way.
    """
    by_name: dict[str, dict[str, Any]] = {}
    for node in vulns:
        name = str(node.get("name") or "").strip().upper()
        if name and node.get("id"):
            by_name.setdefault(name, node)
    coa_name: dict[str, str] = {}
    for rel in remediations or []:
        vuln = (rel.get("to") or {}).get("id")
        name = str((rel.get("from") or {}).get("name") or "").strip()
        if vuln and name and (vuln not in coa_name or name < coa_name[vuln]):
            coa_name[vuln] = name
    out: list[rules.SourceIssue] = []
    for issue in issues:
        named = cves.get(issue.member_id) or []
        if not named:
            out.append(issue)
            continue
        known = [by_name[c] for c in named if c in by_name]
        unknown = tuple(dict.fromkeys(c.upper() for c in named if c not in by_name))
        kev_cves = tuple(
            dict.fromkeys(c.upper() for c in named if c in by_name and by_name[c].get("x_opencti_cisa_kev"))
        )
        cvss = [v for n in known if (v := _float(n.get("x_opencti_cvss_base_score"))) is not None]
        epss = [v for n in known if (v := _float(n.get("x_opencti_epss_score"))) is not None]
        fixes = [
            f for n in known if (f := fixed_versions.version_from_course_of_action(coa_name.get(n["id"]))) is not None
        ]
        out.append(
            replace(
                issue,
                cve=named[0],
                kev=True if kev_cves else (None if unknown else False),
                kev_cves=kev_cves,
                unknown_cves=unknown,
                cvss=max(cvss) if cvss else None,
                epss=max(epss) if epss else None,
                fixed_version=_highest(fixes),
            )
        )
    return out


async def _read_opencti_by_name(
    client: httpx.AsyncClient, base_url: str, headers: dict[str, str], cves: list[str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]] | None:
    """The OpenCTI Vulnerabilities named *cves* and their courses of action, ``None`` when OpenCTI fails."""
    nodes: list[dict[str, Any]] = []
    for start in range(0, len(cves), _PAGE):
        chunk = cves[start : start + _PAGE]
        data, err = await _graphql(
            client,
            base_url,
            headers,
            _OCTI_VULNS_BY_NAME,
            {"filters": _fg([{"key": ["name"], "values": chunk}]), "first": len(chunk) * 2},
        )
        if err:
            logger.info("CTEM cases: OpenCTI signal CVE lookup unavailable: %s", err)
            return None
        nodes.extend(
            node
            for e in ((data or {}).get("vulnerabilities") or {}).get("edges") or []
            if (node := (e or {}).get("node"))
        )
    remediations, _fixes = await _remediations_of(
        client, base_url, headers, sorted({str(n["id"]) for n in nodes if n.get("id")})
    )
    return nodes, remediations


async def _enrich_signals(
    db: AsyncSession, user_id: str, issues: list[rules.SourceIssue], cves: dict[str, list[str]]
) -> list[rules.SourceIssue]:
    """*issues* enriched from OpenCTI with the viewer's own credentials (#2041); unchanged when it cannot answer."""
    wanted = list(dict.fromkeys(c for i in issues for c in cves.get(i.member_id) or ()))[:SIGNAL_CVE_CAP]
    if not wanted:
        return issues
    try:
        cred = await _user_creds(db, user_id, "opencti")
    except Exception as exc:  # unreadable credentials: the facts stay unknown, the members stay
        logger.info("CTEM cases: OpenCTI access unreadable for %s: %s", user_id, format_exception_message(exc))
        return enrich_signal_issues(issues, cves, [])
    if cred is None:
        return enrich_signal_issues(issues, cves, [])
    base_url, headers = cred
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            found = await _read_opencti_by_name(client, base_url, attach_probe(headers, base_url), wanted)
    except Exception as exc:
        logger.warning("CTEM cases: OpenCTI signal CVE lookup failed: %s", format_exception_message(exc))
        found = None
    vulns, remediations = found or ([], [])
    return enrich_signal_issues(issues, cves, vulns, remediations)


async def recompute_for_user(
    db: AsyncSession, *, organization_id: uuid.UUID, user_id: str, vertical: str, now: datetime | None = None
) -> dict[str, Any]:
    """Merge the open issues *user_id* sees into the shared cases; commits.

    Merge only (#11613): what the caller sees open is added or reopened; a
    member is resolved only when the caller's own read confirms it closed (they
    can read it by id and it is no longer open). A member the caller cannot
    see is left as it is, so a narrower view never closes or shrinks a case,
    and the answer counts the caller's own view only.

    A product that could not answer is skipped whole: when any source is
    missing the recompute writes nothing and answers why. A read that hit its
    cap resolves nothing.
    """
    from app.services.ctem_case_loop import PendingEvents, publish_pending
    from app.services.ctem_cases_view import forget_view

    now = now or datetime.now(UTC)
    vertical = normalize_vertical(vertical)
    members = await store.member_ids(db, organization_id=organization_id, vertical=vertical)
    read = await read_open_issues(db, user_id, vertical, now=now, members=members, remap=True)
    answer: dict[str, Any] = {
        "vertical": vertical,
        "missing": read.missing,
        "reasons": dict(read.reasons),
        "no_stack": read.no_stack,
    }
    if read.missing:
        answer["applied"] = False
        return answer
    # #1953: the write phase reads nothing remote and waits briefly for a row lock. The gate's remote
    # reads are taken before the first signal row is written; a row another writer holds (a case
    # delete) fails this pass fast, and the next pass retries, instead of a statement timeout.
    from app.services import ctem_scope_gate_store as gate_store

    await gate_store.prepare_system_reads(db, organization_id)
    await db.execute(text("SELECT set_config('lock_timeout', :timeout, true)"), {"timeout": WRITE_LOCK_TIMEOUT})
    # #2084: a case-opening drain pass writes the same signal rows; the vertical's writer lock keeps
    # the two apart (under lock_timeout: a running drain fails this pass fast, the next one retries).
    from app.services.ctem_case_opening import hold_vertical_lock

    await hold_vertical_lock(db, vertical)
    # OpenAEV #9233 re-keyed every finding: the members it answers 404 for move onto their new
    # finding (or are retired) before the merge, so a case keeps its history and never holds the
    # old and the new id of one finding; a case left with no member resolves.
    pending = PendingEvents()
    remapped = await store.remap_members(
        db,
        organization_id=organization_id,
        vertical=vertical,
        remap=read.remap,
        retired_line=RETIRED_BY_IDENTITY_MIGRATION,
        events=pending,
        now=now,
    )
    counts = await store.apply_recompute(
        db,
        organization_id=organization_id,
        vertical=vertical,
        issues=read.issues,
        closed=[i.member_id for i in read.closed] if read.complete else [],
        events=pending,
        now=now,
    )
    await db.commit()
    await forget_view(str(user_id), vertical)
    # After the commit: the loop's listeners start a run on the cases this read opened or moved.
    await publish_pending(pending)
    answer.update(applied=True, issues=len(read.issues), **counts)
    answer["identity_remap"] = remapped
    # Members the declared version keeps out of every case (CTEM-11), listed with the reason.
    answer["not_affected_issues"] = [
        {"issue": i.member_id, "name": i.name, "product": i.product, "reason": i.not_affected}
        for i in read.issues
        if i.not_affected
    ][:50]
    # What OpenCTI could not tell about a member's versions (#946), never guessed.
    answer["version_notes"] = [
        {"issue": i.member_id, "name": i.name, "product": i.product, "note": i.version_note}
        for i in read.issues
        if i.version_note
    ][:50]
    return answer
