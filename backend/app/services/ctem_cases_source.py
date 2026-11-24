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

import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.utils import format_exception_message
from app.integrations.providers.openaev._common import _api, _search_body
from app.integrations.providers.opencti._common import _graphql
from app.integrations.providers.opencti.capabilities import attach_probe
from app.services import ctem_cases as rules
from app.services import ctem_cases_store as store
from app.services import ctem_fixed_version as fixed_versions
from app.services.ctem_cases import VERSION_AFFECTED, VERSION_NOT_AFFECTED, VERSION_UNKNOWN
from app.services.ctem_metrics_collector import (
    _AEV_VALIDATION_WINDOW_DAYS,
    _STACK_SOFTWARE_CAP,
    _fg,
    _organization_software,
    _stack_matches,
    _user_creds,
)
from app.services.ctem_verticals import VERTICAL_CYBER, normalize_vertical

logger = logging.getLogger(__name__)

#: Most vulnerability memberships (software x vulnerability) read from OpenCTI.
OCTI_ISSUE_CAP = 2000
#: Most CVE findings read from OpenAEV.
AEV_FINDING_CAP = 2000
_PAGE = 500

# The stack's Software observables with their version (CTEM-11, #946): the
# declared version of a stack entry is the Software node of that version.
_OCTI_STACK_SOFTWARE = """
query CtemCaseStackSoftware($filters: FilterGroup, $first: Int) {
  stixCyberObservables(types: ["Software"], filters: $filters, first: $first) {
    edges { node { id ... on Software { name vendor version cpe } } }
  }
}
"""

# The Software versions OpenCTI says remediate each vulnerability (CTEM-11, #946):
# the fixed version is read from OpenCTI, never computed from ranges here.
_OCTI_SOFTWARE_REMEDIATES = """
query CtemCaseSoftwareRemediates($to: [String], $first: Int) {
  stixCoreRelationships(toId: $to, relationship_type: ["remediates"], fromTypes: ["Software"], first: $first) {
    edges { node {
      from { ... on Software { id name vendor version cpe } }
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

#: Most closed-member lookups by id one OpenAEV read makes (the rest wait for the next read).
AEV_LOOKUP_CAP = 200

_AEV_CVE_FINDINGS_FILTER = {
    "mode": "and",
    "filters": [{"key": "finding_type", "mode": "or", "operator": "eq", "values": ["cve"]}],
}

#: The creation date of a Finding as OpenAEV's ``/api/findings/search`` filters it
#: (its ``@Queryable`` field is ``finding_created_at``; ``base_created_at`` is
#: refused with a 400, #12134).
_AEV_FINDING_CREATED_AT = "finding_created_at"


@dataclass
class SourceRead:
    """The open issues read for one recompute, and the products that could not answer."""

    issues: list[rules.SourceIssue] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    no_stack: bool = False
    #: Members the reader can read by id that are no longer open.
    closed: list[rules.SourceIssue] = field(default_factory=list)
    #: False when a read hit its cap: then nothing is resolved from it.
    complete: bool = True


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
    # The OpenCTI Software ids of each installed version, per stack entry.
    version_ids: dict[tuple[str, str], set[str]] = {}
    for node in software:
        label = _stack_matches(node, stack)
        if not label or not node.get("id"):
            continue
        entry = entry_by_label[label]
        product_of[node["id"]] = entry
        versions = _installed_versions(entry)
        for version in versions:
            # A CPE match names the version only when one version runs (the CPE carries no version of its own).
            if fixed_versions.same_version(node.get("version"), version) or (
                len(versions) == 1
                and entry.get("cpe")
                and (node.get("cpe") or "").strip().lower() == entry["cpe"].lower()
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
        vuln, entry = vuln_of[vuln_id], entry_by_label[label]
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
        not_affected: str | None = None
        notes: list[str] = []
        unknown = [v for v, o in outcomes if o == VERSION_UNKNOWN]
        if unknown:
            notes.append(f"{fixed_versions.VERSION_NOT_KNOWN}: {', '.join(unknown)}")
        elif outcomes and all(o == VERSION_NOT_AFFECTED for _v, o in outcomes):
            not_affected = (
                f"{fixed_versions.NOT_AFFECTED_PREFIX} {', '.join(versions)} has no 'has' relation to {name} in OpenCTI"
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
                version_outcomes=tuple(outcomes),
            )
        )
    return out


def openaev_issues(findings: list[dict[str, Any]]) -> list[rules.SourceIssue]:
    """Build the OpenAEV issues from raw findings of type ``cve`` (pure)."""
    out: list[rules.SourceIssue] = []
    seen: set[str] = set()
    for f in findings:
        fid = f.get("finding_id")
        value = (f.get("finding_value") or "").strip()
        if not fid or not value or fid in seen:
            continue
        seen.add(fid)
        assets = tuple(
            dict.fromkeys(
                str(a.get("asset_name") or a.get("asset_id"))
                for a in (f.get("finding_assets") or [])
                if isinstance(a, dict) and (a.get("asset_name") or a.get("asset_id"))
            )
        )
        out.append(
            rules.SourceIssue(
                source="openaev",
                external_id=str(fid)[:200],
                name=value[:300],
                cve=value if value.upper().startswith("CVE-") else None,
                assets=assets,
                first_seen_at=_when(f.get("finding_created_at") or f.get("base_created_at")),
                **_finding_lifecycle(f),
            )
        )
    return out


#: OpenAEV finding statuses (CTEM-14) as the case reads them.
_FINDING_STATUSES = {"OPEN": "open", "NOT_REPRODUCED": "not_reproduced", "RESOLVED": "resolved"}


def _finding_lifecycle(f: dict[str, Any]) -> dict[str, Any]:
    """The re-test status of a finding, feature-detected: an OpenAEV without the lifecycle answers none."""
    status = _FINDING_STATUSES.get(str(f.get("finding_status") or "").upper())
    tested = _when(f.get("finding_last_tested"))
    return {
        "retest_status": status,
        "last_tested_at": tested.isoformat() if tested else None,
        "retest_pending": bool(f.get("finding_retest_inject_id")),
    }


async def _read_opencti(
    client: httpx.AsyncClient,
    base_url: str,
    headers: dict[str, str],
    stack: list[dict[str, Any]],
) -> list[rules.SourceIssue] | None:
    """The open OpenCTI issues of *stack*, read with the caller's own *headers* (never a system token)."""
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
        logger.info("CTEM cases: OpenCTI stack software unavailable: %s", err)
        return None
    software = [
        (e or {}).get("node") or {} for e in ((data or {}).get("stixCyberObservables") or {}).get("edges") or []
    ]
    ids = [n["id"] for n in software if n.get("id") and _stack_matches(n, stack)]
    if not ids:
        return []
    relations: list[dict[str, Any]] = []
    after: str | None = None
    while len(relations) < OCTI_ISSUE_CAP:
        data, err = await _graphql(
            client, base_url, headers, _OCTI_STACK_VULNS, {"from": ids, "first": _PAGE, "after": after}
        )
        if err:
            logger.info("CTEM cases: OpenCTI stack vulnerabilities unavailable: %s", err)
            return None
        conn = (data or {}).get("stixCoreRelationships") or {}
        relations.extend((e or {}).get("node") or {} for e in conn.get("edges") or [])
        info = conn.get("pageInfo") or {}
        after = info.get("endCursor")
        if not info.get("hasNextPage") or not after:
            break
    relations = relations[:OCTI_ISSUE_CAP]
    vuln_ids = sorted({(r.get("to") or {}).get("id") for r in relations if (r.get("to") or {}).get("id")})
    remediations: list[dict[str, Any]] = []
    fixes: list[dict[str, Any]] = []
    for start in range(0, len(vuln_ids), _PAGE):
        batch = {"to": vuln_ids[start : start + _PAGE], "first": _PAGE * 2}
        data, err = await _graphql(client, base_url, headers, _OCTI_REMEDIATIONS, batch)
        if err:  # no course of action is a known state: R2 still answers
            logger.info("CTEM cases: OpenCTI courses of action unavailable: %s", err)
        else:
            remediations.extend(
                (e or {}).get("node") or {}
                for e in ((data or {}).get("stixCoreRelationships") or {}).get("edges") or []
            )
        data, err = await _graphql(client, base_url, headers, _OCTI_SOFTWARE_REMEDIATES, batch)
        if err:  # no remediating Software is a known state: the fixed version stays unknown
            logger.info("CTEM cases: OpenCTI remediating software unavailable: %s", err)
        else:
            fixes.extend(
                (e or {}).get("node") or {}
                for e in ((data or {}).get("stixCoreRelationships") or {}).get("edges") or []
            )
    return opencti_issues(software, stack, relations, remediations, fixes)


async def _read_openaev(
    client: httpx.AsyncClient, base_url: str, headers: dict[str, str], now: datetime
) -> list[rules.SourceIssue] | None:
    since = (now - timedelta(days=_AEV_VALIDATION_WINDOW_DAYS)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    window = {
        "mode": "and",
        "filters": [
            *_AEV_CVE_FINDINGS_FILTER["filters"],
            {"key": _AEV_FINDING_CREATED_AT, "mode": "and", "operator": "gte", "values": [since]},
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
            return None
        body = resp.json() or {}
        content = body.get("content") or []
        findings.extend(f for f in content if isinstance(f, dict))
        page += 1
        if not content or page >= int(body.get("totalPages") or 0):
            break
    return openaev_issues(findings[:AEV_FINDING_CAP])


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
) -> list[rules.SourceIssue] | None:
    """The findings among *ids* the reader can read; a 403 or 404 is not visible, not an outage."""
    found: list[dict[str, Any]] = []
    for fid in ids[:AEV_LOOKUP_CAP]:
        resp, err = await _api(
            base_url, headers, "get", f"/api/findings/{fid}", "get finding", client=client, retry_transient=True
        )
        if resp is not None and resp.status_code == 200:
            body = resp.json()
            if isinstance(body, dict):
                found.append(body)
            continue
        if err and not any(code in err for code in ("403", "404")):
            logger.info("CTEM cases: OpenAEV lookup by id unavailable: %s", err)
            return None
    return openaev_issues(found)


async def read_open_issues(
    db: AsyncSession,
    user_id: str,
    vertical: str,
    *,
    now: datetime | None = None,
    members: list[str] | None = None,
) -> SourceRead:
    """The open issues *user_id* can see in *vertical* (cyber only; other verticals read nothing).

    With *members* (``source:external_id`` of the shared cases), the members
    that are not among the open issues are looked up by id with the same
    credentials: the ones the reader can still read are :attr:`SourceRead.closed`.
    """
    now = now or datetime.now(UTC)
    out = SourceRead()
    if normalize_vertical(vertical) != VERTICAL_CYBER:
        return out
    stack = await _organization_software(db)
    out.no_stack = not stack
    creds: dict[str, tuple[str, dict[str, str]] | None] = {}
    for provider in ("opencti", "openaev"):
        try:
            creds[provider] = await _user_creds(db, user_id, provider)
        except Exception as exc:  # one product failing never blocks the other
            logger.warning("CTEM cases: creds error for %s: %s", provider, format_exception_message(exc))
            creds[provider] = None
    octi = creds.get("opencti")
    if octi and stack:
        base_url, headers = octi
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                found = await _read_opencti(client, base_url, attach_probe(headers, base_url), stack)
        except Exception as exc:
            logger.warning("CTEM cases: OpenCTI read failed: %s", format_exception_message(exc))
            found = None
        if found is None:
            out.missing.append("opencti")
        else:
            out.issues.extend(found)
            out.complete = out.complete and len(found) < OCTI_ISSUE_CAP
    elif not octi:
        out.missing.append("opencti")
    aev = creds.get("openaev")
    if aev:
        base_url, headers = aev
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                found = await _read_openaev(client, base_url, headers, now)
        except Exception as exc:
            logger.warning("CTEM cases: OpenAEV read failed: %s", format_exception_message(exc))
            found = None
        if found is None:
            out.missing.append("openaev")
        else:
            out.issues.extend(found)
            out.complete = out.complete and len(found) < AEV_FINDING_CAP
    else:
        out.missing.append("openaev")
    if members:
        await _read_closed_members(out, creds, members)
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
    readers = {"opencti": _read_opencti_by_id, "openaev": _read_openaev_by_id}
    for source, ids in wanted.items():
        reader = readers.get(source)
        cred = creds.get(source)
        if reader is None or cred is None:
            continue
        base_url, headers = cred
        if source == "opencti":
            headers = attach_probe(headers, base_url)
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                found = await reader(client, base_url, headers, sorted(ids))
        except Exception as exc:
            logger.warning("CTEM cases: %s lookup by id failed: %s", source, format_exception_message(exc))
            found = None
        if found is None:
            out.complete = False
            continue
        if source == "openaev" and len(ids) > AEV_LOOKUP_CAP:
            out.complete = False
        out.closed.extend(found)


async def read_view(db: AsyncSession, user_id: str, vertical: str) -> Any:
    """One viewer's view of the shared cases (``ctem_cases_view.ViewerView``) from their own read."""
    from app.services.ctem_cases_view import ViewerView
    from app.services.organizations import get_primary

    vertical = normalize_vertical(vertical)
    primary = await get_primary(db)
    members = await store.member_ids(db, organization_id=primary.id, vertical=vertical) if primary else []
    read = await read_open_issues(db, user_id, vertical, members=members)
    view = ViewerView(missing=list(read.missing), complete=read.complete)
    view.opencrq = await _reads_opencrq(db, user_id)
    view.opencti = await _reads_opencti(db, user_id)
    for issue in read.closed:
        view.issues[issue.member_id] = issue
    for issue in read.issues:
        view.issues[issue.member_id] = issue
        view.open_ids.add(issue.member_id)
    if primary is not None:
        for issue in await _signal_members(db, organization_id=primary.id, user_id=user_id):
            view.issues[issue.member_id] = issue
            view.open_ids.add(issue.member_id)
    return view


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


async def _signal_members(db: AsyncSession, *, organization_id: uuid.UUID, user_id: str) -> list[rules.SourceIssue]:
    """The signals attached to cases that *user_id* may read (#857), as case members.

    The signal API's own rule: an administrator reads every signal of the
    organization, anyone else the signals recorded for them. A case opened from
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
    if not getattr(viewer, "is_admin", False):
        statement = statement.where(CtemSignal.recorded_by == str(user_id))
    rows = (await db.execute(statement)).scalars().all()
    return [
        rules.SourceIssue(
            source="signal",
            external_id=str(s.id),
            name=(s.subject_label or s.subject_ref or s.trigger_path)[:300],
            assets=(s.subject_ref,) if s.subject_kind == "asset" else (),
            crown_jewels=tuple(
                str(j.get("name"))
                for j in ((s.decision or {}).get("scope") or {}).get("sources") or []
                if isinstance(j, dict) and j.get("from") == "crown_jewel" and j.get("name")
            ),
            first_seen_at=s.first_seen_at,
        )
        for s in rows
    ]


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
    read = await read_open_issues(db, user_id, vertical, now=now, members=members)
    answer: dict[str, Any] = {"vertical": vertical, "missing": read.missing, "no_stack": read.no_stack}
    if read.missing:
        answer["applied"] = False
        return answer
    pending = PendingEvents()
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
