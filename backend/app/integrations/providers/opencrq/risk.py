"""OpenCRQ risk analysis and threat relevance tools.

* ``list_opencrq_risk_scenarios``   - risk scenarios with their latest run (optionally per org).
* ``get_opencrq_risk_scenario``     - scenario detail + mapped controls.
* ``create_opencrq_risk_scenario``  - create a risk scenario.
* ``update_opencrq_risk_scenario``  - update a risk scenario.
* ``delete_opencrq_risk_scenarios`` - bulk-delete risk scenarios.
* ``get_opencrq_scenario_exposure`` - derived supporting assets, highest impact first.
* ``get_opencrq_cve_exploitation``   - how a CVE would be exploited on a scenario's assets.
* ``get_opencrq_attack_path_reach``  - what a finding on a supporting asset reaches along the attack paths.
* ``list_opencrq_attack_outcomes``  - attack outcome taxonomy.
* ``get_opencrq_attack_outcome``    - outcome detail + MITRE techniques.
* ``get_opencrq_financial_summary`` - CRQ financial impact of a scenario.
* ``get_opencrq_threat_relevance_settings``    - threat relevance settings.
* ``update_opencrq_threat_relevance_settings`` - tune threat relevance.
"""

from __future__ import annotations

import logging
import math
import re
import uuid
from typing import Any

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import (
    TENANT_PROP,
    _clean_ids,
    _client,
    _fmt_amount,
    _fmt_rate,
    _fmt_risk_scenario,
    _one_line,
    _quarantined,
    _resolve_tenant,
    _trpc,
)

logger = logging.getLogger(__name__)

_SOURCE = "integration:opencrq"

_CONFIDENCE_GATES = [0, 20, 40, 60, 80, 100]
_HALF_LIFE_DAYS = (7, 3650)
# MIN_TOLERANCE_EXCEEDANCE_PROBABILITY in OpenCRQ: the loss distribution keeps no level beyond 0.999.
_MIN_EXCEEDANCE_PROBABILITY = 0.001
# The supporting assets one exposure read shows: the first page of the walk OpenCRQ ranks by impact.
_EXPOSURE_PAGE_SIZE = 30
# The assets one CVE exploitation read lists; the summary per exposure counts the whole walk.
_EXPLOITATION_PAGE_SIZE = 30
_CVE_ID = re.compile(r"^CVE-\d{4}-\d{4,}$")
# The hops and reached assets one attack-path read lists; the header counts them all.
_ATTACK_PATH_PAGE_SIZE = 40
_EXPOSURE_LABELS = {"internet_facing": "internet-facing", "internal": "internal", "unknown": "exposure unknown"}


def _exposure_label(value: object) -> str:
    """An OpenCRQ asset exposure in words; a value this provider does not know is named as it came."""
    return _EXPOSURE_LABELS.get(value, _one_line(value, 20)) if isinstance(value, str) else "exposure unknown"


def _is_count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _fmt_exploitation_reading(reading: object) -> str:
    """Why OpenCRQ answers these techniques: a mapping naming the CVE, or the topology rule's line.

    ``ExploitationReadingSchema`` of ``risk-scenarios-router.ts``; a kind an older or newer OpenCRQ
    sends that this provider does not know is named as it came.
    """
    if not isinstance(reading, dict):
        return "reading unknown"
    kind = reading.get("kind")
    if kind == "named":
        lanes = ", ".join(_one_line(lane, 20) for lane in reading.get("lanes") or [])
        return f"mapping that names this CVE ({lanes})" if lanes else "mapping that names this CVE"
    if kind == "none":
        return "no mapping names this CVE and the topology rule does not read its vector"
    if kind == "topology":
        rule = reading.get("reading") or {}
        if rule.get("kind") == "weakness":
            return f"topology rule, weakness {_one_line(rule.get('cwe'), 20)}"
        if rule.get("kind") == "vector":
            return (
                "topology rule, CVSS "
                f"AV:{_one_line(rule.get('attackVector'), 4)} "
                f"UI:{_one_line(rule.get('userInteraction'), 4)} "
                f"PR:{_one_line(rule.get('privilegesRequired'), 4)} "
                f"on {_exposure_label(rule.get('exposure'))}"
            )
        return "topology rule"
    return f"reading {_one_line(kind, 20)}"


def _fmt_exploitation_techniques(techniques: object) -> str:
    """The techniques in attack order, each with the lane and the confidence OpenCRQ gives it."""
    items = techniques if isinstance(techniques, list) else []
    if not items:
        return "no technique"
    return " then ".join(
        f"{_one_line(t.get('techniqueExternalId'), 20)} {_one_line(t.get('techniqueName'), 120)}"
        f" ({_one_line(t.get('lane'), 20)}, {_one_line(t.get('confidence'), 10)} confidence)"
        for t in items
        if isinstance(t, dict)
    )


def _fmt_cve_exploitation(answer: dict[str, Any]) -> tuple[str, str]:
    """The header (counts, outside the quarantine) and the body (names and readings, inside it)."""
    cve = answer.get("cve") or {}
    assets = [a for a in (answer.get("assets") or []) if isinstance(a, dict)][:_EXPLOITATION_PAGE_SIZE]
    answered = answer.get("total")
    total = max(answered, len(assets)) if isinstance(answered, int) and _is_count(answered) else len(assets)
    lines = []
    title = _one_line(cve.get("cveTitle"), 200)
    score = cve.get("cvss")
    lines.append(
        f"CVE: {_one_line(cve.get('cveId'), 30)}"
        + (f" - {title}" if title else "")
        + (f" (CVSS {score})" if isinstance(score, (int, float)) and not isinstance(score, bool) else "")
    )
    if cve.get("cvssVector"):
        lines.append(f"  Vector: {_one_line(cve['cvssVector'], 120)}")
    by_exposure = [e for e in (answer.get("byExposure") or []) if isinstance(e, dict)]
    if by_exposure:
        lines.append("  By exposure:")
        for e in by_exposure:
            count = e.get("assetCount")
            label = _exposure_label(e.get("exposure"))
            lines.append(
                f"    - {label}"
                + (f", {count} asset(s)" if _is_count(count) else "")
                + f": {_fmt_exploitation_techniques(e.get('techniques'))}; {_fmt_exploitation_reading(e.get('reading'))}"
            )
    if assets:
        lines.append("  Assets (highest impact first):")
        for a in assets:
            name = (
                _one_line(a.get("supportingAssetName"), 200)
                or _one_line(a.get("supportingAssetDerivedName"), 200)
                or a.get("supportingAssetId")
            )
            primary = ", ".join(
                _one_line(p.get("name"), 120) or str(p.get("id", ""))
                for p in a.get("primaryAssets") or []
                if isinstance(p, dict)
            )
            details = [_exposure_label(a.get("exposure"))]
            if isinstance(a.get("impact"), (int, float)) and not isinstance(a.get("impact"), bool):
                details.append(f"impact {a['impact']:.2f}")
            if a.get("depth") is not None:
                details.append(f"depth {a.get('depth')}")
            if primary:
                details.append(f"primary assets {primary}")
            techniques = _fmt_exploitation_techniques(a.get("techniques"))
            reading = _fmt_exploitation_reading(a.get("reading"))
            lines.append(f"    - {name} ({', '.join(details)}): {techniques}; {reading}")
        if total > len(assets):
            lines.append(f"    ... and {total - len(assets)} more.")
    else:
        lines.append("  Assets: the scenario reaches no supporting asset.")
    return f"Exploitation on the scenario's assets ({total} reached):", "\n".join(lines)


def _attack_path_asset_name(asset: object) -> str:
    """An asset of the attack-path reach by its name, else its derived name, else its id."""
    if not isinstance(asset, dict):
        return "unknown asset"
    return (
        _one_line(asset.get("name"), 200)
        or _one_line(asset.get("derivedName"), 200)
        or _one_line(asset.get("assetId"), 60)
        or "unknown asset"
    )


def _fmt_attack_path_reach(answer: dict[str, Any]) -> tuple[str, str]:
    """The header (counts, outside the quarantine) and the body (asset names, inside it).

    ``getAttackPathReach`` of OpenCRQ's supporting-assets router (043206a5): a hop runs from
    ``toAssetId`` (the side nearer the origin) to ``fromAssetId`` (the asset it reaches), proven by
    an OpenAEV execution or inferred, with the simulation that showed it.
    """
    origin_value = answer.get("origin")
    origin: dict[str, Any] = origin_value if isinstance(origin_value, dict) else {}
    reached = [a for a in (answer.get("reachedAssets") or []) if isinstance(a, dict)]
    hops = [h for h in (answer.get("hops") or []) if isinstance(h, dict)]
    primary = [p for p in (answer.get("primaryAssets") or []) if isinstance(p, dict)]
    names = {str(origin.get("assetId")): _attack_path_asset_name(origin)}
    names.update({str(a.get("assetId")): _attack_path_asset_name(a) for a in reached})
    proven = sum(1 for h in hops if h.get("nature") == "proven")
    lines = [
        f"Origin: {names[str(origin.get('assetId'))]} ({_exposure_label(origin.get('exposure'))}"
        + (f", criticality {_one_line(origin.get('criticality'), 20)}" if origin.get("criticality") else "")
        + ")"
    ]
    if hops:
        lines.append("  Hops (nearest first):")
        for h in hops[:_ATTACK_PATH_PAGE_SIZE]:
            source = names.get(str(h.get("toAssetId")), _one_line(h.get("toAssetId"), 60))
            target = names.get(str(h.get("fromAssetId")), _one_line(h.get("fromAssetId"), 60))
            nature = "proven" if h.get("nature") == "proven" else "inferred"
            evidence = f", simulation {_one_line(h.get('evidenceRef'), 60)}" if h.get("evidenceRef") else ""
            lines.append(
                f"    - {source} -> {target} ({nature}{evidence}, last reported {_one_line(h.get('lastReportedAt'), 30)})"
            )
        if len(hops) > _ATTACK_PATH_PAGE_SIZE:
            lines.append(f"    ... and {len(hops) - _ATTACK_PATH_PAGE_SIZE} more.")
    else:
        lines.append("  Hops: no attack path leaves this asset.")
    if reached:
        lines.append("  Reached assets:")
        for a in reached[:_ATTACK_PATH_PAGE_SIZE]:
            details = [f"depth {a.get('depth')}", _exposure_label(a.get("exposure"))]
            if a.get("criticality"):
                details.append(f"criticality {_one_line(a.get('criticality'), 20)}")
            lines.append(f"    - {names[str(a.get('assetId'))]} ({', '.join(details)})")
        if len(reached) > _ATTACK_PATH_PAGE_SIZE:
            lines.append(f"    ... and {len(reached) - _ATTACK_PATH_PAGE_SIZE} more.")
    if primary:
        lines.append("  Primary assets behind (blast radius):")
        for p in primary[:_ATTACK_PATH_PAGE_SIZE]:
            through = ", ".join(names.get(str(i), _one_line(i, 60)) for i in (p.get("throughAssetIds") or []))
            label = _one_line(p.get("primaryAssetName"), 200) or _one_line(p.get("primaryAssetId"), 60)
            lines.append(f"    - {label}" + (f" (through {through})" if through else ""))
    else:
        lines.append("  Primary assets behind: none reached.")
    stale = answer.get("staleAfterDays")
    if _is_count(stale):
        lines.append(f"  Hops unconfirmed for {stale} days are left out as stale.")
    header = (
        f"Attack-path reach ({len(reached)} asset(s) reached, {len(hops)} hop(s), {proven} proven, "
        f"{len(primary)} primary asset(s) behind):"
    )
    return header, "\n".join(lines)


def _fmt_threat_relevance_settings(settings: dict) -> str:
    """The threat relevance settings, one labelled line per setting OpenCRQ returns.

    ``threat-relevance-settings-router.ts`` (``ThreatRelevanceSettingsOutputSchema``);
    a key an older OpenCRQ does not return gets no line.
    """
    lines = ["Threat relevance settings:"]
    for key, label in (
        ("actHalfLifeObservedDays", "Half-life of observed threat activity"),
        ("actHalfLifeAuthoredDays", "Half-life of authored threat activity"),
    ):
        if key in settings:
            days = settings[key]
            lines.append(f"  {label}: " + (f"{days} days" if days is not None else "not set"))
    if "confidenceGate" in settings:
        gate = settings["confidenceGate"]
        lines.append(
            "  Confidence gate: "
            + (
                f"{gate} (only threat claims at this confidence or above are read)"
                if gate is not None
                else "not set"
            )
        )
    return "\n".join(lines)


def register(
    base_url: str, headers: dict[str, str], executor: CallableToolExecutor
) -> list[ToolDefinition]:
    async def list_opencrq_risk_scenarios(
        organization_id: str = "", tenant_id: str = ""
    ) -> str:
        tenant, err = await _resolve_tenant(base_url, headers, tenant_id)
        if err:
            return err
        payload: dict = {}
        if organization_id:
            payload["organizationId"] = organization_id
        scenarios, err = await _trpc(
            base_url, headers, "riskScenarios.getRiskScenariosOverview",
            "list risk scenarios", input=payload, tenant_id=tenant,
        )
        if err:
            return err
        scenarios = scenarios or []
        if not scenarios:
            return "No risk scenarios found."
        shown = scenarios[:20]
        return _quarantined(
            f"Risk scenarios ({len(scenarios)}, showing {len(shown)}):",
            "\n\n".join(_fmt_risk_scenario(s, base_url, tenant) for s in shown),
            "risk scenarios",
        )

    async def get_opencrq_risk_scenario(
        risk_scenario_id: str = "", tenant_id: str = ""
    ) -> str:
        if not risk_scenario_id:
            return "risk_scenario_id is required."
        # One client for the whole multi-call handler (tenant resolution +
        # scenario + controls) so the calls share a connection pool.
        async with _client(headers, timeout=30) as client:
            tenant, err = await _resolve_tenant(
                base_url, headers, tenant_id, client=client
            )
            if err:
                return err
            scenario, err = await _trpc(
                base_url, headers, "riskScenarios.getRiskScenario",
                "get risk scenario",
                input={"id": risk_scenario_id}, tenant_id=tenant, client=client,
            )
            if err:
                return err
            if not scenario:
                return f"Risk scenario {risk_scenario_id} not found."
            out = _fmt_risk_scenario(scenario, base_url, tenant)
            controls, controls_err = await _trpc(
                base_url, headers,
                "riskScenarios.getControlCapabilitiesForRiskScenario",
                "list scenario control capabilities",
                input={"riskScenarioId": risk_scenario_id}, tenant_id=tenant,
                client=client,
            )
        notes: list[str] = []
        if controls_err:
            # Surface the failure explicitly — a silently missing section
            # would be indistinguishable from "no mitigating controls" and
            # could mislead a gap analysis.
            notes.append(f"Mitigating control capabilities: lookup failed - {controls_err}")
        elif controls:
            lines = []
            for c in controls[:30]:
                if not c.get("isAssessed"):
                    posture = "not assessed"
                else:
                    posture = (
                        f"strength {c.get('strengthRung')}, "
                        f"coverage {c.get('coverageBucket')}"
                    )
                lines.append(
                    f"    - {_one_line(c.get('externalId'), 40, '?')} {_one_line(c.get('name'), 200)} ({posture})"
                    f" (id: {c.get('id')})"
                )
            out += (
                f"\n  Mitigating control capabilities ({len(controls)}):\n"
                + "\n".join(lines)
            )
            if len(controls) > 30:
                out += f"\n    ... and {len(controls) - 30} more."
        else:
            out += "\n  Mitigating control capabilities: none mapped"
        return _quarantined("", out, "risk scenario", *notes)

    async def create_opencrq_risk_scenario(
        organization_id: str = "",
        name: str = "",
        attack_outcome_id: str = "",
        description: str = "",
        primary_asset_ids: list[str] | None = None,
        owner_id: str = "",
        tenant_id: str = "",
    ) -> str:
        if not organization_id or not name or not attack_outcome_id:
            return "organization_id, name, and attack_outcome_id are required."
        tenant, err = await _resolve_tenant(base_url, headers, tenant_id)
        if err:
            return err
        scenario_id = str(uuid.uuid4())
        _, err = await _trpc(
            base_url, headers, "riskScenarios.createRiskScenario",
            "create risk scenario",
            input={
                "id": scenario_id,
                "organizationId": organization_id,
                "name": name,
                "description": description or None,
                "attackOutcomeId": attack_outcome_id,
                "primaryAssetIds": _clean_ids(primary_asset_ids),
                **({"ownerId": owner_id} if owner_id else {}),
            },
            mutation=True,
            tenant_id=tenant,
        )
        if err:
            return err
        return f"Created risk scenario '{name}' (id: {scenario_id})."

    async def update_opencrq_risk_scenario(
        risk_scenario_id: str = "",
        name: str = "",
        description: str | None = None,
        primary_asset_ids: list[str] | None = None,
        owner_id: str | None = None,
        tenant_id: str = "",
    ) -> str:
        if not risk_scenario_id:
            return "risk_scenario_id is required."
        payload: dict = {"id": risk_scenario_id}
        if name:
            payload["name"] = name
        if description is not None:
            payload["description"] = description or None
        if primary_asset_ids is not None:
            payload["primaryAssetIds"] = _clean_ids(primary_asset_ids)
        if owner_id is not None:
            payload["ownerId"] = owner_id or None
        if len(payload) == 1:
            return "Provide at least one field to update."
        tenant, err = await _resolve_tenant(base_url, headers, tenant_id)
        if err:
            return err
        _, err = await _trpc(
            base_url, headers, "riskScenarios.updateRiskScenario",
            "update risk scenario", input=payload, mutation=True, tenant_id=tenant,
        )
        if err:
            return err
        return f"Updated risk scenario {risk_scenario_id}."

    async def list_opencrq_attack_outcomes(tenant_id: str = "") -> str:
        tenant, err = await _resolve_tenant(base_url, headers, tenant_id)
        if err:
            return err
        outcomes, err = await _trpc(
            base_url, headers, "attackOutcomes.getAttackOutcomes",
            "list attack outcomes", tenant_id=tenant,
        )
        if err:
            return err
        outcomes = outcomes or []
        if not outcomes:
            return "No attack outcomes found."
        return _quarantined(
            f"Attack outcomes ({len(outcomes)}):",
            "\n".join(f"- {_one_line(o.get('name'), 200, '?')} (id: {o.get('id')})" for o in outcomes[:50]),
            "attack outcomes",
        )

    async def get_opencrq_financial_summary(
        risk_scenario_id: str = "", tenant_id: str = ""
    ) -> str:
        if not risk_scenario_id:
            return "risk_scenario_id is required."
        # One client for the whole multi-call handler (tenant resolution +
        # summary + estimates) so the calls share a connection pool.
        async with _client(headers, timeout=30) as client:
            tenant, err = await _resolve_tenant(
                base_url, headers, tenant_id, client=client
            )
            if err:
                return err
            summary, err = await _trpc(
                base_url, headers, "scenarioLossEstimates.getFinancialSummary",
                "get financial summary",
                input={"riskScenarioId": risk_scenario_id}, tenant_id=tenant,
                client=client,
            )
            if err:
                return err
            estimates, estimates_err = await _trpc(
                base_url, headers,
                "scenarioLossEstimates.getScenarioLossEstimates",
                "list loss estimates",
                input={"riskScenarioId": risk_scenario_id}, tenant_id=tenant,
                client=client,
            )
        # OpenCRQ answers zero totals, not an error, for a scenario with no estimate.
        if not estimates_err and not estimates:
            return "No loss estimates recorded for this risk scenario."
        summary = summary or {}
        notes: list[str] = []
        lines = [
            f"  Total loss: min {_fmt_amount(summary.get('totalMinDollar'))} / "
            f"likely {_fmt_amount(summary.get('totalLikelyDollar'))} / "
            f"max {_fmt_amount(summary.get('totalMaxDollar'))}, "
            f"PERT expected {_fmt_amount(summary.get('pertExpectedValue'))}",
        ]
        if summary.get("updatedAt"):
            lines.append(f"  Updated: {str(summary['updatedAt'])[:10]}")
        if summary.get("recomputePending"):
            lines.append("  Recompute pending: these figures change once OpenCRQ recomputes them.")
        for w in summary.get("sanityWarnings") or []:
            lines.append(
                f"  Sanity warning ({_one_line(w.get('kind'), 40, 'unknown').replace('_', '-')}): "
                f"expected total {_fmt_amount(w.get('expectedTotalUsd'))} against an "
                f"anchor of {_fmt_amount(w.get('anchorUsd'))} (ratio {_fmt_rate(w.get('ratio'))})"
            )
        if estimates_err:
            notes.append(f"Loss estimates: lookup failed - {estimates_err}")
        elif estimates:
            lines.append(f"  Loss estimates ({len(estimates)}):")
            for e in estimates[:15]:
                driver = _one_line((e.get("lossDriver") or {}).get("name"), 200) or str(
                    e.get("lossDriverId", "unknown driver")
                )
                lines.append(
                    f"    - {driver}: min {_fmt_amount(e.get('minValue'))} / "
                    f"likely {_fmt_amount(e.get('likelyValue'))} / "
                    f"max {_fmt_amount(e.get('maxValue'))}"
                )
            if len(estimates) > 15:
                lines.append(f"    ... and {len(estimates) - 15} more.")
        return _quarantined(
            f"Financial impact summary for risk scenario {risk_scenario_id}:",
            "\n".join(lines),
            "financial summary (loss drivers named in OpenCRQ)",
            *notes,
        )

    async def delete_opencrq_risk_scenarios(
        risk_scenario_ids: list[str] | None = None, tenant_id: str = ""
    ) -> str:
        ids = _clean_ids(risk_scenario_ids)
        if not ids:
            return "risk_scenario_ids is required (one or more scenario UUIDs)."
        tenant, err = await _resolve_tenant(base_url, headers, tenant_id)
        if err:
            return err
        _, err = await _trpc(
            base_url, headers, "riskScenarios.deleteRiskScenarios",
            "delete risk scenarios", input=ids, mutation=True, tenant_id=tenant,
        )
        if err:
            return err
        return f"Deleted {len(ids)} risk scenario(s)."

    async def get_opencrq_scenario_exposure(
        risk_scenario_id: str = "", tenant_id: str = ""
    ) -> str:
        if not risk_scenario_id:
            return "risk_scenario_id is required."
        # One client for the whole multi-call handler (tenant resolution +
        # derived assets) so the calls share a pool.
        async with _client(headers, timeout=30) as client:
            tenant, err = await _resolve_tenant(
                base_url, headers, tenant_id, client=client
            )
            if err:
                return err
            # One page ranked on the server: the whole walk can reach tens of thousands of assets.
            derived, derived_err = await _trpc(
                base_url, headers, "riskScenarios.listDerivedSupportingAssets",
                "list derived supporting assets",
                input={
                    "riskScenarioId": risk_scenario_id,
                    "page": 1,
                    "limit": _EXPOSURE_PAGE_SIZE,
                    "sortBy": "impact",
                    "sortDesc": True,
                },
                tenant_id=tenant,
                client=client,
            )
        header = f"Exposure analysis for risk scenario {risk_scenario_id}:"
        if derived_err:
            return f"{header}\n  Derived supporting assets: lookup failed - {derived_err}"
        if not derived:
            return f"{header}\n  Derived supporting assets: none"
        lines = []
        reached = ", ".join(
            f"{criterion} {grade}"
            for criterion, grade in (
                (derived.get("attackOutcome") or {}).get("criteriaReached") or {}
            ).items()
            if grade != "none"
        )
        lines.append(
            f"  Attack outcome: {_one_line((derived.get('attackOutcome') or {}).get('name'), 200)}"
            + (f" (reaches {reached})" if reached else "")
        )
        assets = (derived.get("items") or [])[:_EXPOSURE_PAGE_SIZE]
        if not assets:
            lines.append("  Derived supporting assets: none")
            return _quarantined(header, "\n".join(lines), "scenario exposure (attack outcome and supporting asset names)")
        # The counts are the whole walk's; a count missing or below the rows read falls back to them.
        total = derived.get("total")
        total = max(total, len(assets)) if _is_count(total) else len(assets)
        direct, transitive = derived.get("directCount"), derived.get("transitiveCount")
        split = (
            f": {direct} direct, {transitive} transitive"
            if _is_count(direct) and _is_count(transitive)
            else ""
        )
        lines.append(f"  Derived supporting assets ({total}{split}, highest impact first):")
        for a in assets:
            name = (
                _one_line(a.get("supportingAssetName"), 200)
                or _one_line(a.get("supportingAssetDerivedName"), 200)
                or a.get("supportingAssetId")
            )
            via = ", ".join(
                _one_line(v.get("name"), 120) or _one_line(v.get("derivedName"), 120) or str(v.get("id", ""))
                for v in a.get("via") or []
            )
            details = [f"depth {a.get('depth')}"]
            if via:
                details.insert(0, f"via {via}")
            if a.get("roles"):
                details.append(f"roles {', '.join(_one_line(r, 40) for r in a['roles'])}")
            if a.get("impact") is not None:
                details.append(f"impact {a['impact']:.2f}")
            if a.get("outOfScope"):
                details.append("out of scope")
            lines.append(f"    - {name} ({', '.join(details)})")
        if total > len(assets):
            lines.append(f"    ... and {total - len(assets)} more.")
        return _quarantined(header, "\n".join(lines), "scenario exposure (attack outcome and supporting asset names)")

    async def get_opencrq_cve_exploitation(risk_scenario_id: str = "", cve_id: str = "", tenant_id: str = "") -> str:
        if not risk_scenario_id:
            return "risk_scenario_id is required."
        cve = (cve_id or "").strip().upper()
        if not _CVE_ID.match(cve):
            return "cve_id is required, written CVE-YYYY-NNNN (for example CVE-2024-3094)."
        tenant, err = await _resolve_tenant(base_url, headers, tenant_id)
        if err:
            return err
        answer, err = await _trpc(
            base_url,
            headers,
            "riskScenarios.getCveExploitation",
            "read the cve exploitation on the scenario",
            input={"riskScenarioId": risk_scenario_id, "cveId": cve, "limit": _EXPLOITATION_PAGE_SIZE},
            tenant_id=tenant or "",
        )
        if err:
            return err
        if not isinstance(answer, dict):
            return f"OpenCRQ answered no exploitation reading for {cve} on risk scenario {risk_scenario_id}."
        header, body = _fmt_cve_exploitation(answer)
        return _quarantined(
            f"{header.rstrip(':')} for risk scenario {risk_scenario_id}:",
            body,
            "cve exploitation (cve title and supporting asset names)",
        )

    async def get_opencrq_attack_path_reach(supporting_asset_id: str = "", tenant_id: str = "") -> str:
        asset_id = (supporting_asset_id or "").strip()
        try:
            uuid.UUID(asset_id)
        except ValueError:
            return "supporting_asset_id is required, an OpenCRQ supporting asset UUID."
        tenant, err = await _resolve_tenant(base_url, headers, tenant_id)
        if err:
            return err
        answer, err = await _trpc(
            base_url,
            headers,
            "supportingAssets.getAttackPathReach",
            "read the attack-path reach of the supporting asset",
            input={"supportingAssetId": asset_id},
            tenant_id=tenant or "",
        )
        if err:
            return err
        if not isinstance(answer, dict):
            return f"OpenCRQ answered no attack-path reach for supporting asset {asset_id}."
        header, body = _fmt_attack_path_reach(answer)
        return _quarantined(header, body, "attack-path reach (supporting and primary asset names)")

    async def get_opencrq_attack_outcome(
        attack_outcome_id: str = "",
        organization_id: str = "",
        tenant_id: str = "",
    ) -> str:
        if not attack_outcome_id:
            return "attack_outcome_id is required."
        # One client for the whole multi-call handler (tenant resolution +
        # outcome + techniques) so the calls share a connection pool.
        async with _client(headers, timeout=30) as client:
            tenant, err = await _resolve_tenant(
                base_url, headers, tenant_id, client=client
            )
            if err:
                return err
            outcome, err = await _trpc(
                base_url, headers, "attackOutcomes.getAttackOutcome",
                "get attack outcome",
                input={"id": attack_outcome_id}, tenant_id=tenant, client=client,
            )
            if err:
                return err
            if not outcome:
                return f"Attack outcome {attack_outcome_id} not found."
            if organization_id:
                result, tech_err = await _trpc(
                    base_url, headers,
                    "attackOutcomes.getMitreTechniquesForOutcome",
                    "get outcome MITRE techniques",
                    input={
                        "attackOutcomeId": attack_outcome_id,
                        "organizationId": organization_id,
                    },
                    tenant_id=tenant, client=client,
                )
                techniques = (result or {}).get("techniques") or []
                heading = (
                    "MITRE techniques weighted for this organization "
                    f"({len(techniques)}, evidence: "
                    f"{(result or {}).get('evidenceMode')})"
                )
            else:
                techniques, tech_err = await _trpc(
                    base_url, headers,
                    "attackOutcomes.getCuratedMitreTechniquesForOutcome",
                    "get outcome curated MITRE techniques",
                    input={"attackOutcomeId": attack_outcome_id},
                    tenant_id=tenant, client=client,
                )
                techniques = techniques or []
                heading = (
                    f"MITRE techniques curated on this outcome ({len(techniques)}; "
                    "pass organization_id for their weights"
                    " against that organization's threat actors)"
                )
        lines = [f"[AttackOutcome] {_one_line(outcome.get('name'), 200, '?')}"]
        lines.append(f"  ID: {outcome.get('id', 'N/A')}")
        notes: list[str] = []
        if outcome.get("description"):
            lines.append(f"  Description: {_one_line(outcome['description'], 300)}")
        reached = ", ".join(
            f"{criterion} {outcome[f'reaches{criterion.capitalize()}']}"
            for criterion in (
                "availability", "integrity", "confidentiality", "traceability",
            )
            if outcome.get(f"reaches{criterion.capitalize()}") not in (None, "none")
        )
        if reached:
            lines.append(f"  Reaches: {reached}")
        if outcome.get("referenceDwellDays") is not None:
            lines.append(f"  Reference dwell: {outcome['referenceDwellDays']} days")
        if tech_err:
            notes.append(f"MITRE techniques: lookup failed - {tech_err}")
        elif techniques:
            lines.append(f"  {heading}:")
            for t in techniques[:25]:
                detail = [_one_line(t.get("category"), 40, "curated")]
                if t.get("weight") is not None:
                    detail.append(f"weight {t['weight']:.2f}")
                if t.get("revoked"):
                    detail.append("revoked")
                lines.append(
                    f"    - {_one_line(t.get('externalId'), 20)} {_one_line(t.get('name'), 120)} "
                    f"({', '.join(detail)})"
                )
            if len(techniques) > 25:
                lines.append(f"    ... and {len(techniques) - 25} more.")
        else:
            lines.append("  MITRE techniques: none mapped")
        return _quarantined("", "\n".join(lines), "attack outcome", *notes)

    async def get_opencrq_threat_relevance_settings(tenant_id: str = "") -> str:
        tenant, err = await _resolve_tenant(base_url, headers, tenant_id)
        if err:
            return err
        settings, err = await _trpc(
            base_url, headers,
            "threatRelevanceSettings.getThreatRelevanceSettings",
            "get threat relevance settings", tenant_id=tenant,
        )
        if err:
            return err
        if not settings:
            return "No threat relevance settings found."
        return _fmt_threat_relevance_settings(settings)

    async def update_opencrq_threat_relevance_settings(
        act_half_life_observed_days: int | None = None,
        act_half_life_authored_days: int | None = None,
        confidence_gate: int | None = None,
        tenant_id: str = "",
    ) -> str:
        given = {
            "act_half_life_observed_days": ("actHalfLifeObservedDays", act_half_life_observed_days),
            "act_half_life_authored_days": ("actHalfLifeAuthoredDays", act_half_life_authored_days),
            "confidence_gate": ("confidenceGate", confidence_gate),
        }
        given = {name: pair for name, pair in given.items() if pair[1] is not None}
        if not given:
            return (
                "Provide at least one of act_half_life_observed_days, "
                "act_half_life_authored_days or confidence_gate."
            )
        for name, (_, value) in given.items():
            if isinstance(value, bool) or not isinstance(value, int):
                return f"{name} must be an integer."
            if name == "confidence_gate":
                if value not in _CONFIDENCE_GATES:
                    return (
                        "confidence_gate must be one of: "
                        + ", ".join(str(g) for g in _CONFIDENCE_GATES) + "."
                    )
            elif not _HALF_LIFE_DAYS[0] <= value <= _HALF_LIFE_DAYS[1]:
                return (
                    f"{name} must be between "
                    f"{_HALF_LIFE_DAYS[0]} and {_HALF_LIFE_DAYS[1]} days."
                )
        changes = {key: value for key, value in given.values()}
        if (
            act_half_life_observed_days is not None
            and act_half_life_authored_days is not None
            and act_half_life_observed_days > act_half_life_authored_days
        ):
            return (
                "act_half_life_observed_days must not exceed "
                "act_half_life_authored_days."
            )
        # The upsert replaces the whole row and requires all three values.
        async with _client(headers, timeout=30) as client:
            tenant, err = await _resolve_tenant(
                base_url, headers, tenant_id, client=client
            )
            if err:
                return err
            current, err = await _trpc(
                base_url, headers,
                "threatRelevanceSettings.getThreatRelevanceSettings",
                "get threat relevance settings",
                tenant_id=tenant, client=client,
            )
            if err:
                return err
            if not current:
                return "Error: OpenCRQ returned no threat relevance settings to update."
            merged = {**current, **changes}
            if merged["actHalfLifeObservedDays"] > merged["actHalfLifeAuthoredDays"]:
                return (
                    "act_half_life_observed_days must not exceed "
                    "act_half_life_authored_days."
                )
            _, err = await _trpc(
                base_url, headers,
                "threatRelevanceSettings.updateThreatRelevanceSettings",
                "update threat relevance settings",
                input={
                    "actHalfLifeObservedDays": merged["actHalfLifeObservedDays"],
                    "actHalfLifeAuthoredDays": merged["actHalfLifeAuthoredDays"],
                    "confidenceGate": merged["confidenceGate"],
                },
                mutation=True, tenant_id=tenant, client=client,
            )
        if err:
            return err
        return (
            "Updated threat relevance settings: observed half-life "
            f"{merged['actHalfLifeObservedDays']} days, authored half-life "
            f"{merged['actHalfLifeAuthoredDays']} days, confidence gate "
            f"{merged['confidenceGate']}."
        )

    async def update_opencrq_risk_tolerance(
        risk_scenario_id: str = "",
        organization_id: str = "",
        annual_loss: float | None = None,
        max_exceedance_probability: float | None = None,
        clear: bool = False,
        tenant_id: str = "",
    ) -> str:
        scenario_id = (risk_scenario_id or "").strip()
        scope_id = (organization_id or "").strip()
        if bool(scenario_id) == bool(scope_id):
            return "Provide exactly one of risk_scenario_id or organization_id."
        tolerance: dict | None = None
        if clear:
            if scope_id:
                return (
                    "A risk scope's tolerance cannot be cleared, only replaced: "
                    "pass annual_loss and max_exceedance_probability instead."
                )
            if annual_loss is not None or max_exceedance_probability is not None:
                return (
                    "clear removes the tolerance: do not pass annual_loss or "
                    "max_exceedance_probability with it."
                )
        else:
            if annual_loss is None or max_exceedance_probability is None:
                return (
                    "Provide both annual_loss and max_exceedance_probability "
                    "(or clear=true to remove a risk scenario's own tolerance)."
                )
            for name, value in (
                ("annual_loss", annual_loss),
                ("max_exceedance_probability", max_exceedance_probability),
            ):
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    return f"{name} must be a number."
            if not math.isfinite(annual_loss) or annual_loss <= 0:
                return "annual_loss must be above zero (US dollars)."
            if not _MIN_EXCEEDANCE_PROBABILITY <= max_exceedance_probability <= 1:
                return (
                    f"max_exceedance_probability must be a fraction between "
                    f"{_MIN_EXCEEDANCE_PROBABILITY} and 1 (5% is 0.05)."
                )
            tolerance = {
                "annualLoss": annual_loss,
                "maxExceedanceProbability": max_exceedance_probability,
            }
        tenant, err = await _resolve_tenant(base_url, headers, tenant_id)
        if err:
            return err
        if scenario_id:
            procedure, payload = "riskScenarios.setRiskTolerance", {
                "riskScenarioId": scenario_id, "tolerance": tolerance,
            }
        else:
            procedure, payload = "organizations.setRiskTolerance", {
                "organizationId": scope_id, "tolerance": tolerance,
            }
        _, err = await _trpc(
            base_url, headers, procedure, "set risk tolerance",
            input=payload, mutation=True, tenant_id=tenant,
        )
        if err:
            return err
        if tolerance is None:
            return (
                f"Cleared risk scenario {scenario_id}'s own tolerance: it follows "
                "its risk scope's tolerance again."
            )
        said = (
            f"{round(max_exceedance_probability * 100, 6):g}% chance of losing "
            f"more than {_fmt_amount(annual_loss)} in a year"
        )
        if scenario_id:
            return f"Set risk scenario {scenario_id}'s own tolerance: {said}."
        return (
            f"Set the tolerance of risk scope {scope_id}: {said}. It applies to "
            "every scenario of the scope that has no tolerance of its own."
        )

    executor.register("list_opencrq_risk_scenarios", list_opencrq_risk_scenarios)
    executor.register("get_opencrq_risk_scenario", get_opencrq_risk_scenario)
    executor.register("create_opencrq_risk_scenario", create_opencrq_risk_scenario)
    executor.register("update_opencrq_risk_scenario", update_opencrq_risk_scenario)
    executor.register("delete_opencrq_risk_scenarios", delete_opencrq_risk_scenarios)
    executor.register("update_opencrq_risk_tolerance", update_opencrq_risk_tolerance)
    executor.register("get_opencrq_scenario_exposure", get_opencrq_scenario_exposure)
    executor.register("get_opencrq_cve_exploitation", get_opencrq_cve_exploitation)
    executor.register("get_opencrq_attack_path_reach", get_opencrq_attack_path_reach)
    executor.register("list_opencrq_attack_outcomes", list_opencrq_attack_outcomes)
    executor.register("get_opencrq_attack_outcome", get_opencrq_attack_outcome)
    executor.register("get_opencrq_financial_summary", get_opencrq_financial_summary)
    executor.register(
        "get_opencrq_threat_relevance_settings",
        get_opencrq_threat_relevance_settings,
    )
    executor.register(
        "update_opencrq_threat_relevance_settings",
        update_opencrq_threat_relevance_settings,
    )

    _tenant_prop = TENANT_PROP

    return [
        ToolDefinition(
            name="list_opencrq_risk_scenarios",
            description=(
                "List OpenCRQ risk scenarios with their organization, attack "
                "outcome, targeted primary assets, latest simulation (ALE "
                "mean with P10 / P90) and readiness for a simulation. "
                "Optionally filter by organization."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "organization_id": {
                        "type": "string",
                        "description": "Optional organization UUID filter.",
                    },
                    **_tenant_prop,
                },
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="get_opencrq_risk_scenario",
            description=(
                "Get an OpenCRQ risk scenario's full detail including the "
                "mitigating control capabilities with their strength and "
                "coverage (useful for gap and coverage analysis)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "risk_scenario_id": {
                        "type": "string",
                        "description": "Risk scenario UUID.",
                    },
                    **_tenant_prop,
                },
                "required": ["risk_scenario_id"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="create_opencrq_risk_scenario",
            description=(
                "Create a new risk scenario in OpenCRQ. Requires an "
                "organization, a name, and an attack outcome (use "
                "list_opencrq_attack_outcomes to discover). Confirm with the "
                "user before creating."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "organization_id": {
                        "type": "string",
                        "description": "Organization UUID.",
                    },
                    "name": {"type": "string", "description": "Scenario name."},
                    "attack_outcome_id": {
                        "type": "string",
                        "description": "Attack outcome UUID.",
                    },
                    "description": {"type": "string"},
                    "primary_asset_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Targeted primary asset UUIDs.",
                    },
                    "owner_id": {
                        "type": "string",
                        "description": "UUID of the user who owns the scenario (list_opencrq_tenant_users).",
                    },
                    **_tenant_prop,
                },
                "required": ["organization_id", "name", "attack_outcome_id"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="update_opencrq_risk_scenario",
            description=(
                "Update an OpenCRQ risk scenario (name, description, targeted "
                "primary assets, owner). Its attack outcome is fixed at creation. "
                "Confirm with the user before updating."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "risk_scenario_id": {
                        "type": "string",
                        "description": "Risk scenario UUID.",
                    },
                    "name": {"type": "string"},
                    "description": {
                        "type": "string",
                        "description": "An empty string removes the description.",
                    },
                    "primary_asset_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "owner_id": {
                        "type": "string",
                        "description": (
                            "UUID of the user who owns the scenario "
                            "(list_opencrq_tenant_users); an empty string removes the owner."
                        ),
                    },
                    **_tenant_prop,
                },
                "required": ["risk_scenario_id"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="list_opencrq_attack_outcomes",
            description=(
                "List the attack outcome taxonomy used by OpenCRQ risk "
                "scenarios (e.g. data breach, ransomware, fraud)."
            ),
            input_schema={"type": "object", "properties": {**_tenant_prop}},
            source=_SOURCE,
        ),
        ToolDefinition(
            name="get_opencrq_financial_summary",
            description=(
                "Get the cyber risk quantification (CRQ) financial impact "
                "summary and per-driver loss estimates of an OpenCRQ risk "
                "scenario."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "risk_scenario_id": {
                        "type": "string",
                        "description": "Risk scenario UUID.",
                    },
                    **_tenant_prop,
                },
                "required": ["risk_scenario_id"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="delete_opencrq_risk_scenarios",
            description=(
                "Delete one or more OpenCRQ risk scenarios (bulk). This is "
                "irreversible and removes the scenarios with their CRQ data "
                "- explicitly confirm the exact scenarios with the user "
                "before deleting."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "risk_scenario_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Risk scenario UUIDs to delete.",
                    },
                    **_tenant_prop,
                },
                "required": ["risk_scenario_ids"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="update_opencrq_risk_tolerance",
            description=(
                "Set an OpenCRQ risk tolerance: the highest probability the "
                "organisation accepts of a year's loss exceeding an amount "
                "(for example a 5% chance of losing more than 2,500,000 in a "
                "year). Pass either risk_scenario_id (the scenario's own "
                "tolerance) or organization_id (the risk scope's tolerance, "
                "which every scenario of the scope without its own follows), "
                "with annual_loss and max_exceedance_probability together; the "
                "new values replace the previous tolerance. clear=true removes "
                "a risk scenario's own tolerance so it follows its risk scope's "
                "again; a risk scope's tolerance cannot be cleared, only "
                "replaced. Confirm with the user before changing a tolerance."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "risk_scenario_id": {
                        "type": "string",
                        "description": "Risk scenario UUID (its own tolerance).",
                    },
                    "organization_id": {
                        "type": "string",
                        "description": "Organization UUID (the risk scope's tolerance).",
                    },
                    "annual_loss": {
                        "type": "number",
                        "exclusiveMinimum": 0,
                        "description": "Annual loss amount in US dollars, above zero.",
                    },
                    "max_exceedance_probability": {
                        "type": "number",
                        "minimum": _MIN_EXCEEDANCE_PROBABILITY,
                        "maximum": 1,
                        "description": (
                            "Highest accepted probability of the year's loss "
                            "exceeding annual_loss, as a fraction, not a "
                            "percentage: 0.05 is 5%."
                        ),
                    },
                    "clear": {
                        "type": "boolean",
                        "description": (
                            "Remove the risk scenario's own tolerance (risk_scenario_id "
                            "only; send no amount or probability)."
                        ),
                    },
                    **_tenant_prop,
                },
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="get_opencrq_scenario_exposure",
            description=(
                "Get the exposure analysis of an OpenCRQ risk scenario: the "
                "supporting assets derived from its primary assets (via the "
                "dependency graph): how many there are and the 30 with the "
                "highest impact on the scenario's loss."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "risk_scenario_id": {
                        "type": "string",
                        "description": "Risk scenario UUID.",
                    },
                    **_tenant_prop,
                },
                "required": ["risk_scenario_id"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="get_opencrq_cve_exploitation",
            description=(
                "How a CVE would be exploited on an OpenCRQ risk scenario's assets: per "
                "exposure and per supporting asset reached (30 by impact), the ATT&CK "
                "techniques in attack order, the reading that produced them (a mapping "
                "naming the CVE, or the topology rule: T1190 internet-facing, T1210 "
                "internal) and the primary assets behind."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "risk_scenario_id": {
                        "type": "string",
                        "description": "Risk scenario UUID.",
                    },
                    "cve_id": {
                        "type": "string",
                        "description": "CVE identifier, for example CVE-2024-3094.",
                    },
                    **_tenant_prop,
                },
                "required": ["risk_scenario_id", "cve_id"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="get_opencrq_attack_path_reach",
            description=(
                "What a finding on an OpenCRQ supporting asset reaches along the attack paths "
                "OpenAEV showed: the hops nearest first, each proven by an execution or inferred "
                "with its simulation, the assets reached and the primary assets behind them "
                "(the blast radius). Stale hops are left out."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "supporting_asset_id": {
                        "type": "string",
                        "description": "Supporting asset UUID (the asset the finding is on).",
                    },
                    **_tenant_prop,
                },
                "required": ["supporting_asset_id"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="get_opencrq_attack_outcome",
            description=(
                "Get an OpenCRQ attack outcome's detail including its MITRE "
                "ATT&CK techniques. Pass organization_id for the techniques "
                "weighted against that organization's threat actors (needs "
                "a user session; a service account is refused), otherwise "
                "the techniques curated on the outcome are listed."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "attack_outcome_id": {
                        "type": "string",
                        "description": "Attack outcome UUID.",
                    },
                    "organization_id": {
                        "type": "string",
                        "description": (
                            "Organization UUID: weights the techniques "
                            "against its admitted threat actors."
                        ),
                    },
                    **_tenant_prop,
                },
                "required": ["attack_outcome_id"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="get_opencrq_threat_relevance_settings",
            description=(
                "Get the OpenCRQ threat relevance engine settings: the "
                "half-lives of observed and authored threat activity and the "
                "confidence gate."
            ),
            input_schema={"type": "object", "properties": {**_tenant_prop}},
            source=_SOURCE,
        ),
        ToolDefinition(
            name="update_opencrq_threat_relevance_settings",
            description=(
                "Update the OpenCRQ threat relevance engine settings. Omitted "
                "settings keep their current value. These settings affect "
                "the whole tenant's scoring - confirm with the user before "
                "changing them."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "act_half_life_observed_days": {
                        "type": "integer",
                        "minimum": _HALF_LIFE_DAYS[0],
                        "maximum": _HALF_LIFE_DAYS[1],
                        "description": (
                            "Half-life of observed threat activity, in days "
                            "(not above the authored one)."
                        ),
                    },
                    "act_half_life_authored_days": {
                        "type": "integer",
                        "minimum": _HALF_LIFE_DAYS[0],
                        "maximum": _HALF_LIFE_DAYS[1],
                        "description": "Half-life of authored threat activity, in days.",
                    },
                    "confidence_gate": {
                        "type": "integer",
                        "enum": _CONFIDENCE_GATES,
                        "description": (
                            "Confidence gate: only threat claims at this "
                            "confidence level or above are read."
                        ),
                    },
                    **_tenant_prop,
                },
                "anyOf": [
                    {"required": ["act_half_life_observed_days"]},
                    {"required": ["act_half_life_authored_days"]},
                    {"required": ["confidence_gate"]},
                ],
            },
            source=_SOURCE,
        ),
    ]
