# SPDX-License-Identifier: Apache-2.0

"""CTEM verticals: cyber (default), FIMI / disinformation, fraud.

The five CTEM lifecycle stages (scoping -> discovery -> prioritization ->
validation -> remediation) are **universal**; each vertical defines its own KPIs,
data sources and scoring. This module is the single registry consumed by the
collector (`ctem_metrics_collector`), the scorer (`ctem_scoring`) and the API
(`ctem_metrics`) so a user's chosen vertical reshapes the whole command center.

Each vertical declares, per stage:
- ``products``: which connected products feed the stage (primary drives the score
  + full confidence; secondary only enriches the displayed facts).
- ``weight``: composite weight (renormalized over covered stages at runtime).
- ``methodology``: human-readable formula surfaced in the UI popover.
- ``facts``: ordered display facts ``(label, fact_key, unit, kind)`` the API
  renders on the stage card (keys map to what the vertical's collector emits).
"""

from __future__ import annotations

# ── Vertical identifiers ─────────────────────────────────────────────────────
VERTICAL_CYBER = "cyber"
VERTICAL_FIMI = "fimi"
VERTICAL_FRAUD = "fraud"

CTEM_VERTICALS: list[str] = [VERTICAL_CYBER, VERTICAL_FIMI, VERTICAL_FRAUD]
DEFAULT_VERTICAL = VERTICAL_CYBER

VERTICAL_LABELS: dict[str, str] = {
    VERTICAL_CYBER: "Cyber",
    VERTICAL_FIMI: "FIMI / Disinformation",
    VERTICAL_FRAUD: "Fraud",
}

VERTICAL_DESCRIPTIONS: dict[str, str] = {
    VERTICAL_CYBER: "Continuous exposure management of your cyber attack surface.",
    VERTICAL_FIMI: "Exposure to foreign information manipulation & interference (disinformation).",
    VERTICAL_FRAUD: "Exposure to digital fraud, phishing, brand abuse and impersonation.",
}


def normalize_vertical(value: str | None) -> str:
    """Coerce any input to a known vertical, defaulting to cyber."""
    candidate = (value or "").strip().lower()
    return candidate if candidate in CTEM_VERTICALS else DEFAULT_VERTICAL


# Stage keys (kept in sync with ctem_scoring.STAGE_KEYS).
_SCOPING = "scoping"
_DISCOVERY = "discovery"
_PRIORITIZATION = "prioritization"
_VALIDATION = "validation"
_REMEDIATION = "remediation"

# Seeded per-stage AI analyst agent slugs (one agent per stage, vertical-aware).
# The remediation stage agent is branded "Mobilization" per CTEM convention.
# This map is the single source shared by the agent seed and the CTEM API.
CTEM_STAGE_AGENT_SLUGS: dict[str, str] = {
    _SCOPING: "ctem-scoping-analyst",
    _DISCOVERY: "ctem-discovery-analyst",
    _PRIORITIZATION: "ctem-prioritization-analyst",
    _VALIDATION: "ctem-validation-analyst",
    _REMEDIATION: "ctem-mobilization-analyst",
}

# Stage display labels per vertical (the lifecycle word can be reframed per
# vertical, e.g. "Mobilization" reads better than "Remediation" for FIMI).
STAGE_LABELS_BY_VERTICAL: dict[str, dict[str, str]] = {
    VERTICAL_CYBER: {
        _SCOPING: "Scoping",
        _DISCOVERY: "Discovery",
        _PRIORITIZATION: "Prioritization",
        _VALIDATION: "Validation",
        _REMEDIATION: "Remediation",
    },
    VERTICAL_FIMI: {
        _SCOPING: "Scoping",
        _DISCOVERY: "Discovery",
        _PRIORITIZATION: "Prioritization",
        _VALIDATION: "Validation",
        _REMEDIATION: "Mobilization",
    },
    VERTICAL_FRAUD: {
        _SCOPING: "Scoping",
        _DISCOVERY: "Discovery",
        _PRIORITIZATION: "Prioritization",
        _VALIDATION: "Validation",
        _REMEDIATION: "Mobilization",
    },
}

# ── Per-vertical, per-stage scoring config ───────────────────────────────────
# products: {"primary": [...], "secondary": [...]}; weight: float (sum ~= 1.0).
STAGE_PRODUCTS: dict[str, dict[str, dict[str, list[str]]]] = {
    VERTICAL_CYBER: {
        _SCOPING: {"primary": ["opencti", "openaev"], "secondary": []},
        _DISCOVERY: {"primary": ["opencti"], "secondary": []},
        _PRIORITIZATION: {"primary": ["opencti"], "secondary": ["openaev"]},
        _VALIDATION: {"primary": ["opencti", "openaev"], "secondary": []},
        _REMEDIATION: {"primary": ["opencti"], "secondary": ["openaev"]},
    },
    VERTICAL_FIMI: {
        # FIMI is OpenCTI-native (Narrative / Channel / Event / Media-Content).
        _SCOPING: {"primary": ["opencti"], "secondary": []},
        _DISCOVERY: {"primary": ["opencti"], "secondary": []},
        _PRIORITIZATION: {"primary": ["opencti"], "secondary": []},
        _VALIDATION: {"primary": ["opencti"], "secondary": []},
        _REMEDIATION: {"primary": ["opencti"], "secondary": []},
    },
    VERTICAL_FRAUD: {
        # Fraud is built from OpenCTI observables/cases; OpenAEV adds phishing
        # human-response validation when connected.
        _SCOPING: {"primary": ["opencti"], "secondary": []},
        _DISCOVERY: {"primary": ["opencti"], "secondary": []},
        _PRIORITIZATION: {"primary": ["opencti"], "secondary": []},
        _VALIDATION: {"primary": ["opencti", "openaev"], "secondary": []},
        _REMEDIATION: {"primary": ["opencti"], "secondary": ["openaev"]},
    },
}

STAGE_WEIGHTS: dict[str, dict[str, float]] = {
    VERTICAL_CYBER: {
        _SCOPING: 0.15,
        _DISCOVERY: 0.25,
        _PRIORITIZATION: 0.25,
        _VALIDATION: 0.20,
        _REMEDIATION: 0.15,
    },
    VERTICAL_FIMI: {
        _SCOPING: 0.20,
        _DISCOVERY: 0.20,
        _PRIORITIZATION: 0.25,
        _VALIDATION: 0.20,
        _REMEDIATION: 0.15,
    },
    VERTICAL_FRAUD: {
        _SCOPING: 0.15,
        _DISCOVERY: 0.25,
        _PRIORITIZATION: 0.25,
        _VALIDATION: 0.20,
        _REMEDIATION: 0.15,
    },
}

METHODOLOGY: dict[str, dict[str, str]] = {
    VERTICAL_CYBER: {
        _SCOPING: (
            "Attack-surface breadth in scope, blended across both products so "
            "connecting OpenAEV only ADDS scope evidence (never lowers the score). "
            "OpenAEV contributes inventory breadth - managed endpoints, asset "
            "groups, detection sources and the share of core ATT&CK attack "
            "domains (Endpoint, Network, Web App, Email, Data Exfiltration, URL "
            "Filtering, Cloud) that have validation content. OpenCTI contributes "
            "intel-scope completeness (observables, CVEs, indicators, reports "
            "present). The two are averaged, so a rich inventory with no BAS "
            "attack-domain content yet no longer scores worse than OpenCTI alone. "
            "Agent-agnostic by design: agentless assets (network / email / web "
            "targets) are a legitimate part of the attack surface, so endpoint-"
            "agent coverage is NOT used."
        ),
        _DISCOVERY: (
            "Threat-informed defense coverage: the share of the MITRE ATT&CK "
            "techniques actually used by the threats OpenCTI tracks (intrusion "
            "sets, malware, campaigns) that are exercised by OpenAEV simulations "
            "- the untested techniques are the gap. Without OpenAEV, OpenCTI "
            "ATT&CK detection coverage (techniques with an indicator/detection) "
            "is the fallback."
        ),
        _PRIORITIZATION: (
            "Exposure containment: the share of tracked vulnerabilities that are "
            "NOT critical and NOT CISA-KEV known-exploited. The analyst weighs "
            "known-exploited / high-EPSS CVEs, indicator sightings (threats "
            "actually observed) and Priority Intelligence Requirements against the "
            "company's software inventory (Company Context) to decide what to fix first."
        ),
        _VALIDATION: (
            "Blended OpenAEV simulation efficacy (prevention %, detection %, "
            "exercised ATT&CK coverage), backed by detection-stack depth (security "
            "platforms + active collectors) and per-technique validation. Otherwise "
            "OpenCTI detection-rule coverage of ATT&CK as a readiness proxy."
        ),
        _REMEDIATION: (
            "Closing the loop: a blend of the closure rate of OpenCTI response "
            "cases (incident + request-for-information, resolved / total - counted "
            "only when there are cases, so an empty queue is not treated as 100%), "
            "response-automation coverage (enabled playbooks), and, when OpenAEV "
            "is connected, prevention efficacy (the share of simulated attacks "
            "actually blocked). The failed/partial validation backlog and open "
            "findings are the work to drive down."
        ),
    },
    VERTICAL_FIMI: {
        _SCOPING: (
            "Information-space coverage: breadth of narratives, channels, "
            "languages and events tracked in OpenCTI."
        ),
        _DISCOVERY: (
            "Monitoring depth: media content captured and the diversity of "
            "channel platforms under watch."
        ),
        _PRIORITIZATION: (
            "Attribution focus: the share of monitored channels attributed to a "
            "threat actor / intrusion set (vs. anonymous noise)."
        ),
        _VALIDATION: (
            "Corroboration: the share of channels with mapped infrastructure "
            "(publishing to domains / URLs), evidencing real operations."
        ),
        _REMEDIATION: (
            "Response: closure rate of FIMI incident / response cases."
        ),
    },
    VERTICAL_FRAUD: {
        _SCOPING: (
            "Brand & asset coverage: monitored brands (identities), watched "
            "domains and impersonation personas in scope."
        ),
        _DISCOVERY: (
            "Fraud surface discovered: phishing domains / URLs, leaked "
            "credentials & cards, fraudulent personas and phishing infrastructure."
        ),
        _PRIORITIZATION: (
            "Containment: share of fraud indicators that are NOT high-confidence "
            "live threats - how focused remediation can be."
        ),
        _VALIDATION: (
            "Confirmation: validated-malicious share of fraud indicators, proven "
            "by OpenAEV phishing simulations when connected."
        ),
        _REMEDIATION: (
            "Takedown velocity: closure rate of phishing / brand-abuse takedown "
            "cases (Case-RFT)."
        ),
    },
}

# ── Per-vertical display facts ───────────────────────────────────────────────
# (label, fact_key, unit, kind). ``kind`` is "fact" or "derived" (a computed %).
# None-valued facts are filtered out by the API so absent metrics simply hide.
STAGE_FACTS: dict[str, dict[str, list[tuple[str, str, str, str]]]] = {
    VERTICAL_CYBER: {
        # Scoping headline = attack-surface breadth under validation: the share of
        # OpenAEV's core ATT&CK attack domains (Endpoint, Network, Web App, Email,
        # Data Exfiltration, URL Filtering, Cloud) that have validation content -
        # OpenAEV's "security domain power". AGENT-AGNOSTIC on purpose: agentless
        # assets are a legitimate part of the attack surface, so endpoint-agent
        # coverage is NOT used. OpenCTI breadth is secondary context, and scope
        # completeness is the fallback signal when OpenAEV is absent.
        _SCOPING: [
            ("Scope breadth", "scope_breadth_pct", "%", "derived"),
            ("Attack surface coverage", "attack_surface_pct", "%", "derived"),
            ("Endpoints in scope", "endpoints_total", "", "fact"),
            ("Asset groups", "asset_groups_total", "", "fact"),
            ("Attack domains covered", "attack_domains_covered", "", "fact"),
            ("Security platforms", "security_platforms_total", "", "fact"),
            ("CVEs tracked", "vulnerabilities_total", "", "fact"),
            ("Observables", "observables", "", "fact"),
            ("Priority intel requirements", "pirs", "", "fact"),
            ("Scope completeness", "scope_completeness_pct", "%", "derived"),
        ],
        # Discovery headline = threat-informed defense coverage (the share of the
        # techniques tracked threats use that OpenAEV actually exercises); the
        # untested techniques are the gap. Falls back to OpenCTI ATT&CK detection
        # coverage when no BAS data is available.
        _DISCOVERY: [
            ("Threat-informed coverage", "threat_informed_coverage_pct", "%", "derived"),
            ("Detection coverage", "detection_coverage_pct", "%", "derived"),
            ("Threat techniques", "threat_techniques", "", "fact"),
            ("Simulated techniques", "exercised_techniques", "", "fact"),
            ("Untested techniques", "untested_techniques", "", "fact"),
            ("Intrusion sets", "intrusion_sets", "", "fact"),
            ("Threats in PIR scope", "pir_threats", "", "fact"),
            ("Malware", "malwares", "", "fact"),
            ("Valid indicators", "indicators_valid", "", "fact"),
        ],
        # Prioritization headline = exposure containment; the actionable facts lead
        # with exploitability (KEV / critical / high-EPSS), then ground priority in
        # real activity (indicator sightings) and Priority Intelligence Requirements.
        _PRIORITIZATION: [
            ("Known-exploited (KEV)", "kev_count", "", "fact"),
            ("Critical CVEs", "critical_count", "", "fact"),
            ("High-EPSS CVEs", "high_epss_count", "", "fact"),
            ("Indicator sightings", "sightings_total", "", "fact"),
            ("Priority intel requirements", "pirs", "", "fact"),
            ("Total CVEs", "vulnerabilities_total", "", "fact"),
            ("Findings", "findings_total", "", "fact"),
        ],
        # Validation headline = OpenAEV efficacy (prevention vs detection) +
        # exercised ATT&CK, backed by detection-stack depth (security platforms /
        # collectors). Detection-rule coverage is the OpenCTI-only fallback signal.
        _VALIDATION: [
            ("Prevention success", "prevention_success_pct", "%", "derived"),
            ("Detection success", "detection_success_pct", "%", "derived"),
            ("ATT&CK exercised", "exercised_coverage_pct", "%", "derived"),
            ("Validated techniques", "techniques_validated", "", "fact"),
            ("Finished simulations", "finished_simulations", "", "fact"),
            # Completeness guard on the two success rates above: OpenAEV excludes
            # pending expectations from its averages, so a high pending share
            # means those rates rest on a small answered subset.
            ("Pending expectations", "expectation_pending_pct", "%", "derived"),
            # Content hygiene: simulations whose stored expectations no longer
            # match their injector contracts measured something other than what
            # the current content would measure.
            ("Drifted simulations", "drifted_simulations", "", "fact"),
            ("Detection sources", "security_platforms", "", "fact"),
            ("Active collectors", "collectors_active", "", "fact"),
            ("Detection rule coverage", "detection_rule_coverage_pct", "%", "derived"),
        ],
        # Remediation headline = response-case closure (incident + RFI); automation
        # playbooks + the failed/partial validation backlog are the levers + work.
        _REMEDIATION: [
            ("Case closure", "case_closure_pct", "%", "derived"),
            ("Incident cases", "case_incidents", "", "fact"),
            ("RFI cases", "case_rfis", "", "fact"),
            ("Automation playbooks", "playbooks_running", "", "fact"),
            ("Open findings", "open_findings", "", "fact"),
            ("Failed/partial validations", "failed_partial_expectations", "", "fact"),
        ],
    },
    VERTICAL_FIMI: {
        _SCOPING: [
            ("Information-space coverage", "scope_completeness_pct", "%", "derived"),
            ("Narratives tracked", "narratives_total", "", "fact"),
            ("Channels monitored", "channels_total", "", "fact"),
            ("Events", "events_total", "", "fact"),
            ("Languages", "languages_total", "", "fact"),
        ],
        _DISCOVERY: [
            ("Monitoring depth", "monitoring_depth_pct", "%", "derived"),
            ("Media content", "media_total", "", "fact"),
            ("Channel platforms", "channel_platforms", "", "fact"),
            ("Narratives", "narratives_total", "", "fact"),
        ],
        _PRIORITIZATION: [
            ("Attributed channels", "attribution_pct", "%", "derived"),
            ("Channels attributed", "channels_attributed", "", "fact"),
            ("Election events", "election_events", "", "fact"),
            ("Channels monitored", "channels_total", "", "fact"),
        ],
        _VALIDATION: [
            ("Corroborated channels", "corroboration_pct", "%", "derived"),
            ("Channels with infrastructure", "channels_with_infra", "", "fact"),
            ("Incidents", "incidents", "", "fact"),
        ],
        _REMEDIATION: [
            ("Case closure", "case_closure_pct", "%", "derived"),
            ("Incident cases", "case_incidents", "", "fact"),
            ("Open incidents", "incidents", "", "fact"),
        ],
    },
    VERTICAL_FRAUD: {
        _SCOPING: [
            ("Brand & asset coverage", "scope_completeness_pct", "%", "derived"),
            ("Brands monitored", "brands_total", "", "fact"),
            ("Domains watched", "domains_total", "", "fact"),
            ("Impersonation personas", "personas_total", "", "fact"),
        ],
        _DISCOVERY: [
            ("Fraud surface discovered", "discovery_pct", "%", "derived"),
            ("Phishing domains/URLs", "phishing_assets", "", "fact"),
            ("Leaked credentials", "credentials_total", "", "fact"),
            ("Leaked cards", "cards_total", "", "fact"),
            ("Phishing infrastructure", "phishing_infra", "", "fact"),
        ],
        _PRIORITIZATION: [
            ("Containment", "containment_pct", "%", "derived"),
            ("Live phishing infra", "phishing_infra", "", "fact"),
            ("Leaked cards", "cards_total", "", "fact"),
            ("Leaked credentials", "credentials_total", "", "fact"),
        ],
        _VALIDATION: [
            ("Confirmed malicious", "confirmed_pct", "%", "derived"),
            ("Validated indicators", "indicators_valid", "", "fact"),
            ("Phishing prevention", "prevention_success_pct", "%", "derived"),
        ],
        _REMEDIATION: [
            ("Takedown closure", "case_closure_pct", "%", "derived"),
            ("Takedown cases", "takedown_cases", "", "fact"),
            ("Open takedowns", "takedowns_open", "", "fact"),
        ],
    },
}
