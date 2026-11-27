"""OpenCRQ findings and posture tools (read-only).

A finding is one vulnerability (a CVE, or an ATT&CK technique a simulation
did not stop) on one supporting asset, scored 0..100 by OpenCRQ and banded
critical / high / medium / low.

* ``get_opencrq_findings_summary``       - open findings by band, exposure
                                           and age.
* ``get_opencrq_top_vulnerabilities``    - the five worst vulnerabilities.
* ``list_opencrq_findings``              - findings, worst first.
* ``get_opencrq_cve_finding``            - one CVE across the estate.
* ``get_opencrq_asset_posture``          - one supporting asset's posture
                                           score and its findings.
* ``get_opencrq_technique_test_posture`` - what attack simulations held.

OpenCRQ refuses every ``findingScore`` read to a service account.
"""

from __future__ import annotations

import logging
from typing import Any

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import (
    CVE_ID_PATTERN,
    ORGANIZATION_SCOPE_PROP,
    TENANT_PROP,
    _as_int,
    _client,
    _client_only_hint,
    _fmt_empty_page,
    _grc_link,
    _organization_scope,
    _resolve_tenant,
    _scoped,
    _text,
    _trpc,
    _unscored_wording,
)

logger = logging.getLogger(__name__)

_SOURCE = "integration:opencrq"

_USER_SESSION = " Needs a user session (a service account is refused)."

_STATES = ["applicable", "dismissed", "all"]

_SORT_KEYS = [
    "score",
    "lastEvaluatedAt",
    "firstSeenAt",
    "assetName",
    "name",
    "currentState",
]

#: Sort keys read A to Z; OpenCRQ sorts descending unless told otherwise.
_ASCENDING_KEYS = {"assetName", "name", "currentState"}

_MAX_LIMIT = 50

_ASSET_FINDINGS_PAGE = 10

_ESTATE_PROP = {
    "organization_id": {
        "type": "string",
        "description": (
            ORGANIZATION_SCOPE_PROP["organization_id"]["description"]
            + " The findings are narrowed only for an organization that maps "
            "its own assets; one measured over the whole estate answers for "
            "the whole tenant, and the output says which."
        ),
    }
}


def _score(value) -> str:
    # OpenCRQ truncates a finding score for display, never rounds it.
    return "unscored" if value is None else str(int(value))


def _row_score(f: dict[str, Any]) -> str:
    """A finding's score, or why it has none (a technique test is never scored)."""
    if f.get("score") is not None:
        return f"score {_score(f['score'])}"
    return _unscored_wording(f)


def _vulnerability(f: dict) -> str:
    if f.get("cveId"):
        title = f" ({str(f['cveTitle'])[:80]})" if f.get("cveTitle") else ""
        return f"{f['cveId']}{title}"
    if f.get("mitreTechniqueExternalId"):
        name = f.get("mitreTechniqueName") or ""
        expectation = f.get("expectationType")
        suffix = f", {expectation} not held" if expectation else ""
        return f"{f['mitreTechniqueExternalId']} {name}{suffix}".strip()
    return "?"


def _fmt_finding_row(f: dict) -> str:
    """One finding on one line; its id reaches the finding's page, so no link per row."""
    asset = f.get("assetName") or f.get("assetDerivedName") or "unnamed asset"
    band = f" ({f['band']})" if f.get("band") else ""
    line = f"- {_vulnerability(f)} on {asset}: {_row_score(f)}{band}, {f.get('currentState', '?')}"
    context = [
        value
        for value in (
            f.get("assetExposure"),
            f"criticality {f['assetCriticality']}" if f.get("assetCriticality") else "",
        )
        if value
    ]
    if context:
        line += f" [{', '.join(context)}]"
    return line + f" (finding id: {f.get('evidenceId')})"


def _fmt_bands(bands: list[dict]) -> str:
    return ", ".join(
        f"{b.get('band')} {b.get('total', 0)} (+{b.get('newInWindow', 0)} new)"
        for b in bands
    )


def _fmt_tally(tally: dict | None) -> str:
    """Latest test results, one per tested asset and technique."""
    if not tally or not tally.get("total"):
        return "no test result"
    held, total = tally.get("held", 0), tally["total"]
    return f"{held} of {total} results held ({held / total * 100:.0f}%)"


def register(
    base_url: str, headers: dict[str, str], executor: CallableToolExecutor
) -> list[ToolDefinition]:
    async def get_opencrq_findings_summary(
        organization_id: str = "", tenant_id: str = ""
    ) -> str:
        organization_id = _text(organization_id)
        async with _client(headers) as client:
            tenant, err = await _resolve_tenant(
                base_url, headers, _text(tenant_id), client=client
            )
            if err:
                return err
            estate, err = await _organization_scope(
                base_url, headers, tenant, organization_id, estate=True, client=client
            )
            if err:
                return err
            counts, err = await _trpc(
                base_url, headers, "findingScore.getFindingsPanelCounts",
                "get findings counts", input=_scoped({}, organization_id),
                tenant_id=tenant, client=client,
            )
            if err:
                return _client_only_hint(err)
            figures, err = await _trpc(
                base_url, headers, "findingScore.getOpenFindingFigures",
                "get open finding figures", input=_scoped({}, organization_id),
                tenant_id=tenant, client=client,
            )
            if err:
                return _client_only_hint(err)
        counts = counts or {}
        figures = figures or {}
        window = str(counts.get("windowStart") or "")[:10]
        lines = [
            f"Findings summary ({estate}):",
            f"- Open findings: {figures.get('open', counts.get('findings', 0))}"
            f" on {counts.get('impactedAssets', 0)} of "
            f"{counts.get('estateAssetCount', 0)} assets",
            f"- By band: {_fmt_bands(counts.get('bands') or [])}"
            + (f" (new since {window})" if window else ""),
            f"- Critical: {figures.get('critical', 0)}, internet-exposed: "
            f"{figures.get('internetExposed', 0)}, known exploited (CISA KEV): "
            f"{figures.get('knownExploited', 0)}, open for more than 90 days: "
            f"{figures.get('aged', 0)}",
        ]
        if counts.get("pendingVector"):
            lines.append(
                f"- {counts['pendingVector']} findings wait for a CVSS vector "
                "and are not scored yet."
            )
        return "\n".join(lines)

    async def get_opencrq_top_vulnerabilities(
        organization_id: str = "", tenant_id: str = ""
    ) -> str:
        organization_id = _text(organization_id)
        async with _client(headers) as client:
            tenant, err = await _resolve_tenant(
                base_url, headers, _text(tenant_id), client=client
            )
            if err:
                return err
            estate, err = await _organization_scope(
                base_url, headers, tenant, organization_id, estate=True, client=client
            )
            if err:
                return err
            rows, err = await _trpc(
                base_url, headers, "findingScore.getTopVulnerabilities",
                "get top vulnerabilities", input=_scoped({}, organization_id),
                tenant_id=tenant, client=client,
            )
            if err:
                return _client_only_hint(err)
        rows = rows or []
        if not rows:
            return f"No open finding ({estate}): no vulnerability to rank."
        lines = [f"Top {len(rows)} vulnerabilities by finding score ({estate}):"]
        for idx, v in enumerate(rows, 1):
            lines.append(
                f"{idx}. {_vulnerability(v)}: {_row_score(v)}, "
                f"{v.get('impactedAssetCount', 0)} impacted assets"
                + (f", reported by {', '.join(v['sources'])}" if v.get("sources") else "")
            )
            kind = "cve_finding" if v.get("cveId") else "finding"
            target = v.get("cveId") or v.get("worstEvidenceId")
            # A CVE finding is read per organization: the link opens the page
            # on the scope the answer named, not on the whole tenant.
            link = _grc_link(
                base_url, tenant, kind, target,
                organization_id=organization_id if kind == "cve_finding" else None,
            )
            if link:
                lines.append(link)
        return "\n".join(lines)

    async def list_opencrq_findings(
        organization_id: str = "",
        state: str = "applicable",
        cve_id: str = "",
        sort_by: str = "score",
        page: int = 1,
        limit: int = 20,
        tenant_id: str = "",
    ) -> str:
        organization_id = _text(organization_id)
        state = _text(state) or "applicable"
        sort_by = _text(sort_by) or "score"
        cve_id = _text(cve_id).upper()
        if state not in _STATES:
            return "state must be one of: " + ", ".join(_STATES) + "."
        if sort_by not in _SORT_KEYS:
            return "sort_by must be one of: " + ", ".join(_SORT_KEYS) + "."
        if cve_id and not CVE_ID_PATTERN.match(cve_id):
            return "cve_id must be a CVE identifier (e.g. 'CVE-2024-3400')."
        page, limit = _as_int(page), _as_int(limit)
        if page is None or page < 1:
            return "page must be an integer >= 1."
        if limit is None or not 1 <= limit <= _MAX_LIMIT:
            return f"limit must be an integer between 1 and {_MAX_LIMIT}."
        payload: dict = {
            "page": page,
            "limit": limit,
            "sortBy": sort_by,
            "sortDesc": sort_by not in _ASCENDING_KEYS,
            "state": None if state == "all" else state,
        }
        if cve_id:
            payload["vulnerability"] = {"kind": "cve", "cveId": cve_id}
        async with _client(headers) as client:
            tenant, err = await _resolve_tenant(
                base_url, headers, _text(tenant_id), client=client
            )
            if err:
                return err
            estate, err = await _organization_scope(
                base_url, headers, tenant, organization_id, estate=True, client=client
            )
            if err:
                return err
            result, err = await _trpc(
                base_url, headers, "findingScore.listScoredFindings", "list findings",
                input=_scoped(payload, organization_id), tenant_id=tenant, client=client,
            )
            if err:
                return _client_only_hint(err)
        items = (result or {}).get("items") or []
        total = (result or {}).get("total", len(items))
        if not items:
            return _fmt_empty_page(f"No finding matched ({estate}).", page, total)
        lines = [f"Findings ({estate}; page {page}, showing {len(items)} of {total}):"]
        lines += [_fmt_finding_row(f) for f in items]
        unscored = (result or {}).get("unscored") or 0
        if unscored:
            lines.append(f"{unscored} findings are not scored yet.")
        return "\n".join(lines)

    async def get_opencrq_cve_finding(
        cve_id: str = "", organization_id: str = "", tenant_id: str = ""
    ) -> str:
        cve_id = _text(cve_id).upper()
        organization_id = _text(organization_id)
        if not CVE_ID_PATTERN.match(cve_id):
            return "cve_id is required, as a CVE identifier (e.g. 'CVE-2024-3400')."
        async with _client(headers) as client:
            tenant, err = await _resolve_tenant(
                base_url, headers, _text(tenant_id), client=client
            )
            if err:
                return err
            estate, err = await _organization_scope(
                base_url, headers, tenant, organization_id, estate=True, client=client
            )
            if err:
                return err
            f, err = await _trpc(
                base_url, headers, "findingScore.getUniqueCveFinding", "get CVE finding",
                input=_scoped({"cveId": cve_id}, organization_id), tenant_id=tenant,
                client=client,
            )
            if err:
                return _client_only_hint(err)
        if not f:
            return f"No asset has a finding for {cve_id} ({estate})."
        lines = [f"[CVE finding] {_vulnerability(f)}", f"  Estate: {estate}"]
        link = _grc_link(
            base_url, tenant, "cve_finding", cve_id,
            organization_id=organization_id or None,
        )
        if link:
            lines.append(link)
        band = f" ({f['band']})" if f.get("band") else ""
        lines.append(f"  Score: {_score(f.get('score'))}{band}")
        if f.get("worstScore") is not None:
            lines.append(
                f"  Worst single asset: {_score(f['worstScore'])}"
                + (f" ({f['worstBand']})" if f.get("worstBand") else "")
            )
        lines.append(f"  Affected assets: {f.get('affectedAssetCount', 0)}")
        lines.append(f"  Known exploited (CISA KEV): {'yes' if f.get('knownExploited') else 'no'}")
        if f.get("epss") is not None:
            lines.append(f"  EPSS: {f['epss']}")
        if f.get("sources"):
            lines.append(f"  Reported by: {', '.join(f['sources'])}")
        if f.get("firstSeenAt"):
            lines.append(f"  First seen: {str(f['firstSeenAt'])[:10]}")
        if f.get("pendingVector"):
            lines.append("  Waiting for a CVSS vector: not scored yet.")
        return "\n".join(lines)

    async def get_opencrq_asset_posture(
        supporting_asset_id: str = "", page: int = 1, tenant_id: str = ""
    ) -> str:
        supporting_asset_id = _text(supporting_asset_id)
        if not supporting_asset_id:
            return "supporting_asset_id is required."
        page = _as_int(page)
        if page is None or page < 1:
            return "page must be an integer >= 1."
        async with _client(headers) as client:
            tenant, err = await _resolve_tenant(
                base_url, headers, _text(tenant_id), client=client
            )
            if err:
                return err
            posture, err = await _trpc(
                base_url, headers, "findingScore.getAssetPosture",
                "get asset posture",
                input={"supportingAssetId": supporting_asset_id},
                tenant_id=tenant, client=client,
            )
            if err:
                return _client_only_hint(err)
            findings, findings_err = await _trpc(
                base_url, headers, "findingScore.listAssetFindings",
                "list asset findings",
                input={
                    "supportingAssetId": supporting_asset_id,
                    "page": page,
                    "limit": _ASSET_FINDINGS_PAGE,
                    "sortBy": "score",
                },
                tenant_id=tenant, client=client,
            )
        if not posture:
            return (
                f"Supporting asset {supporting_asset_id} has no posture score: "
                "it has no scored applicable finding, or it does not exist."
            )
        items = [] if findings_err else (findings or {}).get("items") or []
        total = (findings or {}).get("total", len(items))
        first = items[0] if items else {}
        asset_name = (
            first.get("assetName") or first.get("assetDerivedName") or "supporting asset"
        )
        lines = [
            f"[Asset posture] {asset_name} (id: {supporting_asset_id})",
            f"  Posture score: {posture.get('score', '?')}/100 (higher is "
            "stronger: 75+ strong, 50-74 moderate, 25-49 weak, under 25 "
            "critical)",
        ]
        bands = posture.get("countsByBand") or {}
        lines.append(
            "  CVE findings by exploitability band: "
            + ", ".join(f"{band} {bands.get(band, 0)}" for band in ("critical", "high", "medium", "low"))
        )
        worst = posture.get("worstFinding") or {}
        if worst:
            lines.append(
                f"  Worst finding: {worst.get('name', '?')}, score "
                f"{_score(worst.get('score'))} (exploitability "
                f"{worst.get('exploitability')}, impact {worst.get('impact')})"
            )
        if findings_err:
            lines.append(f"  (Findings list unavailable: {_client_only_hint(findings_err)})")
        elif items:
            lines.append(
                f"  Findings, worst first (page {page}, showing {len(items)} of {total}):"
            )
            lines += [f"  {_fmt_finding_row(f)}" for f in items]
        elif total:
            lines.append(f"  {_fmt_empty_page('', page, total)}")
        return "\n".join(lines)

    async def get_opencrq_technique_test_posture(
        organization_id: str = "", tenant_id: str = ""
    ) -> str:
        organization_id = _text(organization_id)
        async with _client(headers) as client:
            tenant, err = await _resolve_tenant(
                base_url, headers, _text(tenant_id), client=client
            )
            if err:
                return err
            estate, err = await _organization_scope(
                base_url, headers, tenant, organization_id, estate=True, client=client
            )
            if err:
                return err
            posture, err = await _trpc(
                base_url, headers, "findingScore.getTechniqueTestPosture",
                "get technique test posture", input=_scoped({}, organization_id),
                tenant_id=tenant, client=client,
            )
            if err:
                return _client_only_hint(err)
        posture = posture or {}
        tested = posture.get("testedAssetCount", 0)
        if not tested:
            return f"No asset has been tested by an attack simulation ({estate})."
        lines = [f"Attack simulation results on {tested} tested assets ({estate}):"]
        expectations = posture.get("expectations") or {}
        for kind in ("prevention", "detection"):
            e = expectations.get(kind) or {}
            line = f"- {kind.capitalize()}: {_fmt_tally(e.get('current'))}"
            if e.get("previous"):
                line += f" (30 days ago: {_fmt_tally(e['previous'])})"
            lines.append(line)
        return "\n".join(lines)

    executor.register("get_opencrq_findings_summary", get_opencrq_findings_summary)
    executor.register(
        "get_opencrq_top_vulnerabilities", get_opencrq_top_vulnerabilities
    )
    executor.register("list_opencrq_findings", list_opencrq_findings)
    executor.register("get_opencrq_cve_finding", get_opencrq_cve_finding)
    executor.register("get_opencrq_asset_posture", get_opencrq_asset_posture)
    executor.register(
        "get_opencrq_technique_test_posture", get_opencrq_technique_test_posture
    )

    return [
        ToolDefinition(
            name="get_opencrq_findings_summary",
            description=(
                "Summarize the open vulnerability findings of an OpenCRQ tenant "
                "or organization: count by severity band with what is new, "
                "impacted assets, critical, internet-exposed, known exploited "
                "and aged findings." + _USER_SESSION
            ),
            input_schema={
                "type": "object",
                "properties": {**_ESTATE_PROP, **TENANT_PROP},
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="get_opencrq_top_vulnerabilities",
            description=(
                "Get the five worst vulnerabilities of an OpenCRQ tenant or "
                "organization (CVEs, or ATT&CK techniques a simulation did not "
                "stop), by finding score, with how many assets each one "
                "impacts." + _USER_SESSION
            ),
            input_schema={
                "type": "object",
                "properties": {**_ESTATE_PROP, **TENANT_PROP},
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="list_opencrq_findings",
            description=(
                "List OpenCRQ vulnerability findings (one vulnerability on one "
                "supporting asset), worst score first by default, optionally "
                "for one CVE. Each row gives the score (0-100), band, state, "
                "the asset's exposure and criticality, and the finding id."
                + _USER_SESSION
            ),
            input_schema={
                "type": "object",
                "properties": {
                    **_ESTATE_PROP,
                    "state": {
                        "type": "string",
                        "enum": _STATES,
                        "default": "applicable",
                        "description": "applicable = open findings.",
                    },
                    "cve_id": {
                        "type": "string",
                        "description": "Only the findings of this CVE.",
                    },
                    "sort_by": {
                        "type": "string",
                        "enum": _SORT_KEYS,
                        "default": "score",
                        "description": "Scores and dates sort highest and latest first, names A to Z.",
                    },
                    "page": {"type": "integer", "minimum": 1, "default": 1},
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": _MAX_LIMIT,
                        "default": 20,
                    },
                    **TENANT_PROP,
                },
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="get_opencrq_cve_finding",
            description=(
                "Get one CVE as a finding across the OpenCRQ estate: its finding "
                "score and band, the worst single asset, the number of affected "
                "assets, CISA KEV and EPSS." + _USER_SESSION
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "cve_id": {
                        "type": "string",
                        "description": "CVE identifier, e.g. 'CVE-2024-3400'.",
                    },
                    **_ESTATE_PROP,
                    **TENANT_PROP,
                },
                "required": ["cve_id"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="get_opencrq_asset_posture",
            description=(
                "Get an OpenCRQ supporting asset's posture score (0-100, higher "
                "is stronger), its CVE findings by exploitability band, its "
                f"worst finding, and its findings {_ASSET_FINDINGS_PAGE} per page."
                + _USER_SESSION
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "supporting_asset_id": {
                        "type": "string",
                        "description": "Supporting asset UUID.",
                    },
                    "page": {
                        "type": "integer",
                        "minimum": 1,
                        "default": 1,
                        "description": "Page of the asset's findings.",
                    },
                    **TENANT_PROP,
                },
                "required": ["supporting_asset_id"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="get_opencrq_technique_test_posture",
            description=(
                "Get what attack simulations proved on an OpenCRQ tenant or "
                "organization: of the latest test results, one per tested asset "
                "and ATT&CK technique, how many prevented and detected the "
                "attack, now and 30 days ago." + _USER_SESSION
            ),
            input_schema={
                "type": "object",
                "properties": {**_ESTATE_PROP, **TENANT_PROP},
            },
            source=_SOURCE,
        ),
    ]
