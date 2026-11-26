"""The CTEM sample of ``[service.lab-ctem]``: deterministic OpenCTI facts per day, OpenAEV left out.

Nothing answers the Suite platforms in a lab, so the live collection finds no product and every
stage score is null. Under ``LAB_CTEM_SAMPLE=1`` the lab answers the collection with the facts
below (``lab_asgi``) and writes the earlier days as recorded snapshots (``seed_ctem_history.py``),
so the command center's Coverage matrix and Exposure stats render on real API answers:

- OpenCTI is the only product present: OpenAEV is a primary source of Scoping and Validation (a
  blind spot, with its Connect button) and a secondary one of Prioritization and Remediation;
- Scoping improves, Discovery declines, Prioritization holds, Validation stays null (no OpenAEV,
  no fallback fact), Remediation improves;
- ``DAYS_AGO`` 0 is today, served live; the others are recorded days.

``findings_facts`` is the other sample, of ``[service.lab-ctem-findings]``: OpenCTI and OpenAEV present,
failed expectations on four types, a connected stage with no data, KEVs and a detection coverage gap.

``screens_facts`` and ``SCREENS_ISSUES`` are the sample of ``[service.lab-ctem-screens]`` (#11713): OpenCTI
and OpenAEV present, every stage scored above zero, the funnel's inputs (a live funnel), and the open issues
the CTEM Cases source read answers, so the cases list, the Mobilization board, the stage pages and the
funnel render on real API answers.

``modules_facts`` is the sample of ``[service.lab-ctem-modules-cti]``, ``-cti-aev`` and ``-all`` (#11998): the stage
pages in one, two and three module states, with the Discovery lists (techniques by tactic, top threats, targeting),
the Scoping lists, the Prioritization top vulnerabilities and findings, and with OpenAEV the Validation outcome
matrix rows (#12220); ``-cti-unscored`` (Discovery unscored with OpenCTI alone) and ``-none`` (no module) too.

Standard library only: ``lab_asgi`` and the seed script import it from this directory.
dev-docs/reference/agentic-labs.md, "The xtm-one lab".
"""

from __future__ import annotations

#: The vertical the sample feeds: the default one the command center opens on.
VERTICAL = "cyber"
#: The switches whose lab answers the cyber collection with a sample (or an OpenCTI) of its own: no ``LARGE_FACTS``
#: overlay on them (``lab_asgi.large_ctem_facts``; ``lab.seeds_ctem_large_facts`` names the same list, pinned by
#: test). ``LAB_CTEM_OPENCTI`` keeps the KEV and critical counts its OpenCTI holds (#12220).
CTEM_SAMPLE_SWITCHES: tuple[str, ...] = (
    "LAB_CTEM_FINDINGS", "LAB_CTEM_ACTIONS", "LAB_CTEM_ACTIVITY", "LAB_CTEM_SAMPLE_STATE", "LAB_CTEM_SCREENS",
    "LAB_CTEM_OPENCTI",
)
#: The products the sample reports present.
PRESENT = ("opencti",)


def _day(scoping: float, discovery: float, prioritization: float, closure: float, automation: float) -> dict:
    return {
        "scoping": {"scope_completeness_pct": scoping},
        "discovery": {"detection_coverage_pct": discovery},
        "prioritization": {"noncritical_share_pct": prioritization},
        "remediation": {"case_closure_pct": closure, "playbook_automation_pct": automation},
    }


#: Stage facts by days before today, oldest first; 0 is the live answer.
DAYS_AGO: dict[int, dict[str, dict[str, float]]] = {
    3: _day(55, 60, 58, 48, 56),
    2: _day(63, 55, 60, 52, 60),
    1: _day(68, 51, 61, 54, 62),
    0: _day(72, 48, 61, 55, 65),
}


def stage_facts(days_ago: int) -> dict[str, dict[str, float]]:
    """A fresh copy of the stage facts of one day (callers mutate what the collector answers)."""
    return {stage: dict(facts) for stage, facts in DAYS_AGO[days_ago].items()}


# ``[service.lab-ctem-findings]`` (``LAB_CTEM_FINDINGS=on``): the findings the rule-based rows of the
# Recommended actions card act on, OpenCTI and OpenAEV both present.

#: The products the findings report present: Scoping's primary sources, connected yet silent.
FINDINGS_PRESENT = ("opencti", "openaev")
#: Failed and partial OpenAEV expectations on every type the card names, in the collector's shape.
FINDINGS_EXPECTATION_BUCKETS: dict[str, dict[str, int]] = {
    "PREVENTION": {"SUCCESS": 14, "FAILED": 4, "PARTIAL": 2, "PENDING": 1},
    "DETECTION": {"SUCCESS": 9, "FAILED": 6, "PARTIAL": 1, "PENDING": 0},
    "VULNERABILITY": {"SUCCESS": 5, "FAILED": 3, "PARTIAL": 0, "PENDING": 0},
    "HUMAN_RESPONSE": {"SUCCESS": 2, "FAILED": 1, "PARTIAL": 1, "PENDING": 2},
}


def _success_pct(bucket: dict[str, int]) -> float:
    """The collector's ``_aev_success_pct``: the SUCCESS share of every result."""
    total = sum(bucket.values())
    return round(100 * bucket.get("SUCCESS", 0) / total, 1) if total else 0.0


# Every lab that records the CTEM posture history (``LAB_CTEM_POSTURE_HISTORY=1``): counts of 1,000 and more
# beside small ones on every stage, so a compact-number review (621K, exact on hover) reads them with no override.

#: The count facts of the cyber stages, OpenCTI's only (OpenAEV is not present): a value of each size on each
#: stage, consistent with one another (42 KEVs among 1,240 critical CVEs among 620,965 tracked).
LARGE_FACTS: dict[str, dict[str, int]] = {
    "scoping": {"vulnerabilities_total": 620965, "observables": 1240, "pirs": 42},
    "discovery": {"indicators_valid": 620965, "intrusion_sets": 1240, "threat_techniques": 999, "malwares": 42},
    "prioritization": {
        "vulnerabilities_total": 620965, "sightings_total": 1240000, "critical_count": 1240, "high_epss_count": 999,
        "kev_count": 42, "pirs": 42,
    },
    "validation": {"collectors_active": 1240, "security_platforms": 42},
    "remediation": {"open_findings": 620965, "case_incidents": 1240, "playbooks_running": 999, "case_rfis": 42},
}


def with_large_facts(stage_facts: dict[str, dict]) -> dict[str, dict]:
    """*stage_facts* with :data:`LARGE_FACTS` laid over each stage, a fresh copy (no score reads a count)."""
    merged = {stage: dict(facts) for stage, facts in stage_facts.items()}
    for stage, counts in LARGE_FACTS.items():
        merged.setdefault(stage, {}).update(counts)
    return merged


#: The known-exploited vulnerabilities a saved tech stack narrows the findings' ``kev_count`` (7) to
#: (``LAB_CTEM_KEV_TAILORED=on``, ``lab_asgi.tailored_ctem_posture``).
KEV_TAILORED_COUNT = 3


def tailored_kev(labels: list[str]) -> dict:
    """The Prioritization facts a tech stack of *labels* tailors: the count and the first three products."""
    return {"kev_tailored_count": KEV_TAILORED_COUNT, "kev_tailored_products": list(labels[:3])}


def findings_facts() -> dict[str, dict]:
    """Today's cyber facts of the findings lab, a fresh copy.

    - Scoping answers no fact while its primary sources are present: covered false, no blind spot;
    - Discovery's ATT&CK detection coverage is under 60 (the coverage gap row);
    - Prioritization counts known-exploited exposures (the KEV row);
    - Validation carries the OpenAEV expectation buckets (one row per failed type);
    - Remediation as the sample's today.
    """
    buckets = {kind: dict(bucket) for kind, bucket in FINDINGS_EXPECTATION_BUCKETS.items()}
    return {
        "discovery": {"detection_coverage_pct": 42},
        "prioritization": {
            "noncritical_share_pct": 61, "kev_count": 7, "critical_count": 12, "vulnerabilities_total": 140,
        },
        "validation": {
            "finished_simulations": 6,
            "expectation_buckets": buckets,
            "prevention_success_pct": _success_pct(buckets["PREVENTION"]),
            "detection_success_pct": _success_pct(buckets["DETECTION"]),
        },
        "remediation": {"case_closure_pct": 55, "playbook_automation_pct": 65},
    }


# ``[service.lab-ctem-screens]`` (``LAB_CTEM_SCREENS=on``, #11713): the screens a CTEM UX round reviews.

#: The products the screens sample reports present: every cyber stage has its primary product.
SCREENS_PRESENT = ("opencti", "openaev")
#: The funnel's OpenCTI counts: known, tailored to the stack, then exploitable on each (never increasing).
SCREENS_FUNNEL = {"known": 1840, "exploitable_known": 96, "relevant": 140, "exploitable_relevant": 9}


def screens_facts() -> dict[str, dict]:
    """Today's cyber facts of the screens lab, a fresh copy: five scored stages and the funnel's inputs.

    The collector's ``_attach_funnel`` turns ``funnel_inputs`` and ``funnel_tailored`` into the
    prioritization ``funnel`` (``lab_asgi.screens_ctem_posture``), as on a tenant with both products.
    """
    buckets = {kind: dict(bucket) for kind, bucket in FINDINGS_EXPECTATION_BUCKETS.items()}
    return {
        "scoping": {"scope_completeness_pct": 72, "scope_breadth_pct": 64},
        "discovery": {"detection_coverage_pct": 58, "threat_informed_coverage_pct": 46},
        "prioritization": {
            "noncritical_share_pct": 63, "kev_count": 7, "critical_count": 12,
            "vulnerabilities_total": SCREENS_FUNNEL["known"],
            "funnel_inputs": {"known": SCREENS_FUNNEL["known"], "exploitable": SCREENS_FUNNEL["exploitable_known"]},
            "funnel_tailored": {
                "relevant": SCREENS_FUNNEL["relevant"], "exploitable": SCREENS_FUNNEL["exploitable_relevant"],
            },
        },
        "validation": {
            "finished_simulations": 6,
            "expectation_buckets": buckets,
            "prevention_success_pct": _success_pct(buckets["PREVENTION"]),
            "detection_success_pct": _success_pct(buckets["DETECTION"]),
        },
        "remediation": {"case_closure_pct": 55, "playbook_automation_pct": 65},
    }


# ``[service.lab-ctem-modules-*]`` (``LAB_CTEM_MODULES=cti|cti-aev|all``, #11998): the stage pages in each module
# combination, the Discovery lists included, so a UX round sees the mixed views and the gap fillers on real answers.

#: The products present per ``LAB_CTEM_MODULES`` value (OpenCRQ is registration-driven: ``lab.py`` leaves it out).
MODULE_STATES: dict[str, tuple[str, ...]] = {
    "cti": ("opencti",),
    "cti-aev": ("opencti", "openaev"),
    "all": ("opencti", "openaev", "opencrq"),
    # #12220: OpenCTI alone with no ATT&CK detection coverage, so Discovery scores 0 and the page says it needs
    # OpenAEV (``scoreNeedsOpenAev``); and no module at all, every stage a blind spot.
    "cti-unscored": ("opencti",),
    "none": (),
}

#: The Scoping lists the collector reads from OpenCTI (``_stage_viz``, #12220), in the collector's shape: the
#: observables by entity type (``_named_rows``, top 8, largest first) and the vulnerabilities by CVSS severity as
#: OpenCTI stores it, UNKNOWN included (a CVE with no CVSS score).
SCOPING_MIX: dict[str, list[dict]] = {
    "observables_by_type": [
        {"name": name, "value": value}
        for name, value in (
            ("IPv4-Addr", 48210), ("Domain-Name", 31877), ("Url", 22140), ("StixFile", 18452),
            ("Email-Addr", 6120), ("Hostname", 4310), ("Autonomous-System", 1240), ("IPv6-Addr", 412),
        )
    ],
    "vulnerabilities_by_severity": [
        {"label": label, "value": value}
        for label, value in (("CRITICAL", 12), ("HIGH", 214), ("MEDIUM", 611), ("LOW", 187), ("UNKNOWN", 816))
    ],
}

#: The Prioritization lists the collector reads from OpenCTI (#12220): the CVSS distribution (the same rows as
#: Scoping's) and the top vulnerabilities by CVSS, in ``top_vulnerabilities``' shape.
PRIORITIZATION_MIX: dict[str, list[dict]] = {
    "severity_distribution": [dict(row) for row in SCOPING_MIX["vulnerabilities_by_severity"]],
    "top_vulnerabilities": [
        # ``id``: the row's OpenCTI id, the "Open in OpenCTI" pivot (#1357); a stable lab value per CVE.
        {"id": f"vulnerability--lab-{name.lower()}", "name": name, "cvss": cvss, "severity": severity, "epss": epss, "kev": kev}
        for name, cvss, severity, epss, kev in (
            ("CVE-2024-21762", 9.8, "CRITICAL", 0.94, True),
            ("CVE-2024-23113", 9.8, "CRITICAL", 0.71, True),
            ("CVE-2024-50379", 9.8, "CRITICAL", 0.12, False),
            ("CVE-2023-48788", 9.3, "CRITICAL", 0.42, False),
            ("CVE-2024-21887", 9.1, "CRITICAL", 0.97, True),
            ("CVE-2017-0144", 8.8, "HIGH", 0.97, True),
            ("CVE-2023-46805", 8.2, "HIGH", 0.96, False),
        )
    ],
}

#: OpenAEV's open findings on the assets and those carrying a CVE (``findings_total`` / ``findings_cve``, #12220);
#: the CVE share is the funnel's asset layer, so it stays under the funnel's last OpenCTI layer.
MODULES_FINDINGS = {"findings_total": 89, "findings_cve": 7}

#: The OpenAEV kill chain phases of the Validation outcome matrix (#12220), in the rows the collector reads
#: (``_outcome_matrix``): MITRE ATT&CK, the chain with the most exercised techniques, then a two-phase custom one.
OUTCOME_PHASES: tuple[dict, ...] = tuple(
    {"phase_id": f"phase-lab-{chain}-{order}", "phase_kill_chain_name": chain, "phase_name": name, "phase_order": order}
    for chain, phases in (
        ("mitre-attack", ("initial-access", "execution", "persistence", "privilege-escalation", "defense-evasion",
                          "credential-access", "lateral-movement", "exfiltration")),
        ("lab-custom", ("phishing-drill", "ransomware-drill")),
    )
    for order, name in enumerate(phases, start=1)
)


def _technique(n: int, code: str, name: str, chain: str, orders: tuple[int, ...],
               prevention: tuple[int, int], detection: tuple[int, int]) -> dict:
    return {
        "attack_pattern_id": f"attack-pattern--lab-{n:04d}", "attack_pattern_name": name,
        "attack_pattern_external_id": code,
        "kill_chain_phases": [
            {"phase_id": f"phase-lab-{chain}-{order}", "phase_name": None, "phase_order": order} for order in orders
        ],
        "prevention_success": prevention[0], "prevention_total": prevention[1],
        "detection_success": detection[0], "detection_total": detection[1],
    }


#: The exercised techniques (OpenAEV's coverage rows, #12220): every outcome the matrix draws, prevented,
#: detected only, missed and untested, on both chains.
OUTCOME_ROWS: tuple[dict, ...] = (
    _technique(1, "T1566", "Phishing", "mitre-attack", (1,), (4, 6), (5, 6)),
    _technique(2, "T1190", "Exploit Public-Facing Application", "mitre-attack", (1,), (0, 3), (2, 3)),
    _technique(3, "T1059", "Command and Scripting Interpreter", "mitre-attack", (2,), (0, 5), (0, 5)),
    _technique(4, "T1053", "Scheduled Task/Job", "mitre-attack", (2, 3, 4), (2, 2), (1, 2)),
    _technique(5, "T1547", "Boot or Logon Autostart Execution", "mitre-attack", (3, 4), (0, 0), (0, 0)),
    _technique(6, "T1562", "Impair Defenses", "mitre-attack", (5,), (0, 4), (3, 4)),
    _technique(7, "T1003", "OS Credential Dumping", "mitre-attack", (6,), (1, 4), (2, 4)),
    _technique(8, "T1021", "Remote Services", "mitre-attack", (7,), (0, 2), (0, 2)),
    _technique(9, "T1041", "Exfiltration Over C2 Channel", "mitre-attack", (8,), (3, 3), (3, 3)),
    _technique(10, "LAB-01", "Credential harvesting mail", "lab-custom", (1,), (2, 4), (1, 4)),
    _technique(11, "LAB-02", "File encryption on a share", "lab-custom", (2,), (0, 2), (0, 2)),
)

#: The Discovery lists the collector reads from OpenCTI (``_stage_viz``), in the collector's shape: the tactics in
#: kill chain order as OpenCTI stores them, each list a top-N.
DISCOVERY_MIX: dict[str, list[dict]] = {
    "techniques_by_tactic": [
        {"tactic": tactic, "count": count}
        for tactic, count in (
            ("reconnaissance", 41), ("resource-development", 38), ("initial-access", 62), ("execution", 118),
            ("persistence", 104), ("privilege-escalation", 97), ("defense-evasion", 186), ("credential-access", 74),
            ("discovery", 89), ("lateral-movement", 33), ("collection", 52), ("command-and-control", 71),
            ("exfiltration", 19), ("impact", 15),
        )
    ],
    "top_threats": [
        {"name": name, "value": value}
        for name, value in (("APT29", 142), ("FIN7", 118), ("Lazarus Group", 104), ("APT41", 97), ("Sandworm Team", 81))
    ],
    "target_sectors": [
        {"name": name, "value": value}
        for name, value in (("Finance", 64), ("Government", 58), ("Energy", 41), ("Healthcare", 33), ("Technology", 29))
    ],
    "target_countries": [
        {"name": name, "value": value}
        for name, value in (("United States", 71), ("France", 44), ("Germany", 39), ("United Kingdom", 36), ("Japan", 22))
    ],
}


#: The Validation lists the collector reads from OpenCTI (``_stage_viz``): per tactic of DISCOVERY_MIX, the threat
#: techniques and those with an indicator or a detection rule, and the detection content counts.
VALIDATION_MIX: dict[str, list[dict]] = {
    "rule_coverage_by_tactic": [
        {"tactic": row["tactic"], "total": row["count"], "covered": row["count"] * share // 100}
        for row, share in zip(
            DISCOVERY_MIX["techniques_by_tactic"], (12, 8, 21, 18, 15, 11, 9, 14, 10, 24, 6, 19, 5, 13)
        )
    ],
    "detection_depth": [
        {"name": "indicators", "value": 38500},
        {"name": "detection_indicators", "value": 21400},
        {"name": "sightings", "value": 1820},
    ],
}


#: The Mobilization lists (``_stage_viz``, #11998): OpenCTI's response cases by kind and its closure and
#: automation counts, then OpenAEV's fix backlog (dropped without OpenAEV).
MOBILIZATION_MIX: dict[str, object] = {
    "response_mix": [{"name": "case_incidents", "value": 41}, {"name": "case_rfis", "value": 18}],
    "response_progress": {"cases_closed": 61, "cases_total": 83, "playbooks_running": 3, "playbooks_total": 5},
    "fix_backlog": [{"name": "open_findings", "value": 89}, {"name": "failed_partial_expectations", "value": 330}],
}


def modules_facts(state: str) -> tuple[dict[str, dict], set[str]]:
    """Today's cyber facts and present products of one ``LAB_CTEM_MODULES`` state, a fresh copy.

    The screens sample with the Discovery, Scoping and Prioritization lists; without OpenAEV its facts
    (validation results, the threat-informed coverage, the findings) are dropped, as a tenant with OpenCTI
    alone answers them. ``cti-unscored`` is ``cti`` with no ATT&CK detection coverage; ``none`` answers no
    fact. The outcome matrix rows (``OUTCOME_ROWS``) and the funnel are the collector's own steps, laid by
    ``lab_asgi.modules_ctem_posture``.
    """
    present = set(MODULE_STATES[state])
    if not present:
        return {}, present
    facts = screens_facts()
    facts["scoping"].update({key: [dict(row) for row in rows] for key, rows in SCOPING_MIX.items()})
    facts["prioritization"].update({key: [dict(row) for row in rows] for key, rows in PRIORITIZATION_MIX.items()})
    facts["prioritization"].update(MODULES_FINDINGS)
    facts["discovery"].update({key: [dict(row) for row in rows] for key, rows in DISCOVERY_MIX.items()})
    # The distinct techniques the tracked threats use (the hero's count), at least the largest tactic's.
    facts["discovery"]["threat_techniques"] = 188
    # The Validation lists OpenCTI answers (#11998): rule coverage per tactic, detection depth.
    facts["validation"].update({key: [dict(row) for row in rows] for key, rows in VALIDATION_MIX.items()})
    # The Mobilization lists (#11998), a fresh copy of each.
    facts["remediation"].update(
        {key: [dict(r) for r in v] if isinstance(v, list) else dict(v) for key, v in MOBILIZATION_MIX.items()}
    )
    if "openaev" not in present:
        facts["validation"] = {key: facts["validation"][key] for key in VALIDATION_MIX}
        facts["discovery"].pop("threat_informed_coverage_pct", None)
        facts["remediation"].pop("fix_backlog", None)
        for key in MODULES_FINDINGS:
            facts["prioritization"].pop(key, None)
    if state == "cti-unscored":
        facts["discovery"]["detection_coverage_pct"] = 0
    return facts, present


def _cti(n: int, cve: str, product: str, vendor: str, fixed: str | None, cvss: float, epss: float,
         kev: bool = False, coa: tuple[str, str] | None = None) -> dict:
    return {
        "source": "opencti", "external_id": f"vulnerability--lab-{n:04d}", "name": cve, "cve": cve,
        "product": product, "vendor": vendor, "fixed_version": fixed, "cvss": cvss, "epss": epss, "kev": kev,
        "course_of_action_id": coa[0] if coa else None, "course_of_action_name": coa[1] if coa else None,
        "days_ago": 3 + n,
    }


_SMB = ("course-of-action--lab-0001", "Disable SMBv1 on every file server")

#: The open issues the CTEM Cases source read answers (``ctem_cases.SourceIssue`` fields; ``days_ago`` is the
#: first seen date): four remediation keys (two upgrades with a fixed version, one with none, one course of
#: action), an OpenAEV finding joining a case by its CVE (R3) and one issue with no known fix. Stable ids, so a
#: recompute after a restart merges into the same cases.
SCREENS_ISSUES: tuple[dict, ...] = (
    _cti(1, "CVE-2024-21762", "FortiOS", "Fortinet", "7.2.7", 9.8, 0.94, kev=True),
    _cti(2, "CVE-2024-23113", "FortiOS", "Fortinet", "7.2.7", 9.8, 0.71, kev=True),
    _cti(3, "CVE-2023-48788", "FortiOS", "Fortinet", "7.2.7", 9.3, 0.42),
    _cti(4, "CVE-2024-21887", "Connect Secure", "Ivanti", "22.7R2.3", 9.1, 0.97, kev=True),
    _cti(5, "CVE-2023-46805", "Connect Secure", "Ivanti", "22.7R2.3", 8.2, 0.96),
    _cti(6, "CVE-2024-50379", "Tomcat", "Apache", None, 9.8, 0.12),
    _cti(7, "CVE-2017-0144", "Windows Server", "Microsoft", None, 8.8, 0.97, kev=True, coa=_SMB),
    _cti(8, "CVE-2017-0145", "Windows Server", "Microsoft", None, 8.8, 0.95, coa=_SMB),
    {
        "source": "openaev", "external_id": "finding-lab-0001", "name": "CVE-2024-21762", "cve": "CVE-2024-21762",
        "cvss": 9.8, "assets": ("fw-paris-01", "fw-lyon-02"), "primary_assets": ("Customer SaaS hosting",),
        "days_ago": 2,
        # The case's Re-test column (#2316, gap lab-seed-openaev-finding-lifecycle): tested, still open, re-test queued.
        "retest_status": "open", "last_tested_at": "2026-11-20T09:00:00+00:00", "retest_pending": True,
    },
    {"source": "openaev", "external_id": "finding-lab-0002", "name": "Weak TLS cipher on the VPN gateway",
     "cvss": 5.3, "assets": ("vpn-gw-01",), "days_ago": 1,
     "retest_status": "open", "last_tested_at": "2026-11-22T15:30:00+00:00"},
)
#: The cases a recompute of :data:`SCREENS_ISSUES` creates (one per remediation key).
SCREENS_CASES = 4

#: The issues each viewer's Cases source read leaves out, by the viewer's email: the lab's non-admin peer
#: (``lab.PEER_EMAIL``) reads the Ivanti case without CVE-2023-46805, so for that viewer the case is
#: ``aggregates_restricted`` (an open member read without its details, #1931) while the administrator
#: reads it whole. A UX round of the restricted state signs in as the peer; no ``window.fetch`` override.
SCREENS_RESTRICTED: dict[str, tuple[str, ...]] = {
    "lab-peer@filigran.io": ("opencti:vulnerability--lab-0005",),
}

#: The scope the screens lab declares before its recompute: since #1529 a case opens only through the scope
#: gate, which resolves a vulnerability to the profile's tech stack and the assets running it. Each product of
#: :data:`SCREENS_ISSUES` with a fix is in the stack and runs on a ``very_high`` asset (``critical`` on the scope
#: scale, above the default ``high`` threshold), so each remediation key opens its case.
SCREENS_TECH_STACK: tuple[dict, ...] = (
    {"vendor": "Fortinet", "product": "FortiOS", "cpe": ""},
    {"vendor": "Ivanti", "product": "Connect Secure", "cpe": ""},
    {"vendor": "Apache", "product": "Tomcat", "cpe": ""},
    {"vendor": "Microsoft", "product": "Windows Server", "cpe": ""},
)
#: The asset records that run them (``POST /organization-context/assets/records/batch``, stable external ids).
SCREENS_ASSETS: tuple[dict, ...] = (
    {"name": "fw-paris-01", "category": "network_device", "criticality": "very_high", "internet_facing": "yes",
     "products": ["FortiOS"], "source": "lab", "external_id": "lab-screens-fw-paris-01"},
    {"name": "vpn-gw-01", "category": "network_device", "criticality": "very_high", "internet_facing": "yes",
     "products": ["Connect Secure"], "source": "lab", "external_id": "lab-screens-vpn-gw-01"},
    {"name": "app-portal-01", "category": "host", "criticality": "very_high", "internet_facing": "yes",
     "products": ["Tomcat"], "source": "lab", "external_id": "lab-screens-app-portal-01"},
    {"name": "fs-paris-01", "category": "host", "criticality": "very_high", "internet_facing": "no",
     "products": ["Windows Server"], "source": "lab", "external_id": "lab-screens-fs-paris-01"},
)
#: Primary asset -> the asset records that support it (``PUT /organization-context/primary-assets/{name}/links``,
#: #2316, gap lab-seed-primary-asset-links). The primary asset is added to the profile when it is missing.
SCREENS_PRIMARY_ASSET_LINKS: dict[str, tuple[str, ...]] = {
    "Customer SaaS hosting": ("fw-paris-01", "vpn-gw-01", "app-portal-01"),
}
