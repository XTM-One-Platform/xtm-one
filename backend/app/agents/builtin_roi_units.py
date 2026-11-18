# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""The ROI unit each seeded agent declares (CTEM seeded fleets S4, issue #8990).

``dev-docs/ctem-fleets.md`` section 5: a run of an agent whose ``output_schema``
carries ``x-xtm-roi`` records the units of work it produced, counted from its
structured output. This table is where a built-in agent declares its unit; the
seed (``app.agents.seed``) writes it into the agent's ``output_schema`` on
insert and on every full sync, so the seed JSON files and the inline schemas
stay the provider-facing contract and the declaration lives in one place.

A custom agent declares its unit the same way, in its own ``output_schema``.

Every built-in agent is in exactly one of two tables (``dev-docs/roi-outcomes.md``
section 10.3, #9836 R2): :data:`BUILTIN_ROI_UNITS` when its structured output
carries a countable artefact of a catalog unit, :data:`BUILTIN_ROI_EXEMPT` with
the reason it records none. ``tests/test_builtin_roi_units.py`` fails on a
seeded agent in neither, so a new built-in cannot ship uncounted and
unexplained.
"""

from __future__ import annotations

import copy
from typing import Any

from app.services.roi import BASELINES_BY_UNIT, ROI_SCHEMA_KEYWORD, UnitDeclaration

#: Built-in agent slug -> the unit its runs produce and where the count is read.
BUILTIN_ROI_UNITS: dict[str, UnitDeclaration] = {
    # One briefing per run, its headline the artefact.
    "exposure-briefing-analyst": UnitDeclaration("briefing_written", "headline"),
    # One STIX bundle per harvested document, its id the artefact.
    "cti-stix-harvester": UnitDeclaration("stix_bundle_built", "id"),
    "aev-phishing-email-generator": UnitDeclaration("phishing_content_built", "html_body"),
    "aev-phishing-landing-page-generator": UnitDeclaration("phishing_content_built", "html"),
    # Every rule of the array counts.
    "remediation-crowdstrike-rule-generator": UnitDeclaration("detection_rule_written", "rules"),
    "remediation-splunk-rule-generator": UnitDeclaration("detection_rule_written", "spl_query"),
    # CTEM fleet role agents (F1 seeds them, F2 / F3 reuse them; #9114).
    "ctem-threat-relevance-analyst": UnitDeclaration("threat_report_triaged", "threats"),
    "ctem-risk-quantifier": UnitDeclaration("risk_scenario_quantified", "scenarios"),
    "ctem-remediation-coordinator": UnitDeclaration("remediation_ticket_drafted", "actions"),
    # F3 AI security (#9113): each ATLAS technique mapped, each AI test proposed.
    "ctem-ai-threat-analyst": UnitDeclaration("atlas_mapping", "atlas_techniques"),
    "ctem-ai-security-orchestrator": UnitDeclaration("ai_test_plan", "proposed_tests"),
    # CTEM F2 (#9120): every CVE of the findings array was assessed against the estate.
    "ctem-vuln-triage-analyst": UnitDeclaration("cve_assessed", "findings"),
    # CTEM posture fleet stage analysts (#9836 R2): one stage assessment per run, its headline the artefact.
    "ctem-scoping-analyst": UnitDeclaration("ctem_scope_reviewed", "headline"),
    "ctem-discovery-analyst": UnitDeclaration("exposure_inventory_refreshed", "headline"),
    "ctem-prioritization-analyst": UnitDeclaration("exposure_prioritized", "headline"),
    "ctem-validation-analyst": UnitDeclaration("validation_reviewed", "headline"),
    "ctem-mobilization-analyst": UnitDeclaration("mobilization_plan", "headline"),
    # Indicator decay (#9836 R2): every verdict is one indicator judged.
    "indicator-decay-judge": UnitDeclaration("indicator_decay_reviewed", "verdicts"),
    # OpenCTI PIR desk (#10542): every PIR match triaged; every PIR answered in a report.
    "opencti-pir-triage-analyst": UnitDeclaration("threat_report_triaged", "matches"),
    "opencti-landscape-reporter": UnitDeclaration("briefing_written", "headline"),
    # OpenAEV detection desk (#10575): one simulation analysed per run; every gap proven closed is a re-test.
    "openaev-coverage-gap-analyst": UnitDeclaration("simulation_result_analysed", "simulation"),
    "openaev-coverage-reporter": UnitDeclaration("retest_cycle", "closed_gaps"),
    # OpenCRQ risk budget desk (#10593): every scenario re-priced, every investment case, one Board pack per run.
    "opencrq-register-analyst": UnitDeclaration("risk_scenario_quantified", "scenarios"),
    "opencrq-investment-analyst": UnitDeclaration("investment_case_written", "cases"),
    "opencrq-board-reporter": UnitDeclaration("briefing_written", "headline"),
}

#: Why a built-in agent records no unit of its own.
EXEMPT_ORCHESTRATOR = "orchestrator"  # counts its fleet's outcome through its members, never a second time
EXEMPT_TRANSFORMER = "transformer"  # rewrites another agent's artefact
EXEMPT_UTILITY = "utility"  # a step inside another agent's or a product's flow
EXEMPT_CHAT = "chat"  # answers in chat, counted by D2 when it lands
EXEMPT_REASONS: tuple[str, ...] = (EXEMPT_ORCHESTRATOR, EXEMPT_TRANSFORMER, EXEMPT_UTILITY, EXEMPT_CHAT)

#: Built-in agent slug -> the reason it records no unit (one of :data:`EXEMPT_REASONS`).
BUILTIN_ROI_EXEMPT: dict[str, str] = {
    "xtm-one-assistant": EXEMPT_CHAT,
    "opencti-assistant": EXEMPT_CHAT,
    "openaev-assistant": EXEMPT_CHAT,
    "opencrq-assistant": EXEMPT_CHAT,
    "ctem-assistant": EXEMPT_CHAT,
    "fimi-analyst": EXEMPT_CHAT,
    "deep-investigation-agent": EXEMPT_CHAT,
    "openaev-attack-path-orchestrator": EXEMPT_ORCHESTRATOR,
    "ctem-vuln-prioritization-orchestrator": EXEMPT_ORCHESTRATOR,
    "ctem-third-party-risk-orchestrator": EXEMPT_ORCHESTRATOR,
    "opencti-pir-desk-orchestrator": EXEMPT_ORCHESTRATOR,
    "openaev-detection-desk-orchestrator": EXEMPT_ORCHESTRATOR,
    "opencrq-risk-desk-orchestrator": EXEMPT_ORCHESTRATOR,
    "ctem-threat-response-orchestrator": EXEMPT_ORCHESTRATOR,
    "ctem-posture-orchestrator": EXEMPT_ORCHESTRATOR,
    "indicator-decay-guardian": EXEMPT_ORCHESTRATOR,
    "fimi-stix-transformer": EXEMPT_TRANSFORMER,
    "cti-stix-transformer": EXEMPT_TRANSFORMER,
    "aev-detection-rules-generator": EXEMPT_TRANSFORMER,
    "opencti-nlq": EXEMPT_UTILITY,
    # The grounded extraction (#9126): one tool's bundle, bound to no intent and with
    # no output schema to read a count from until it is evaluated against the harvester.
    "cti-stix-extractor": EXEMPT_UTILITY,
    "opencti-dashboard-widget-creator": EXEMPT_UTILITY,
    "opencti-dashboard-creator": EXEMPT_UTILITY,
    "opencti-detection-rule-converter": EXEMPT_UTILITY,
    "opencti-threat-hunt-creator": EXEMPT_UTILITY,
    "opencti-entity-insights": EXEMPT_UTILITY,
    "fimi-infrastructure-investigator": EXEMPT_UTILITY,
    "openaev-payload-creator": EXEMPT_UTILITY,
    "openaev-scenario-creator": EXEMPT_UTILITY,
    "openaev-scenario-publisher": EXEMPT_UTILITY,
    "remediation-rule-type-detector": EXEMPT_UTILITY,
    "cti-ttp-harvester-filigran-ai": EXEMPT_UTILITY,
    "cti-stix-harvester-filigran-ai": EXEMPT_UTILITY,
    "cti-stix-consumer": EXEMPT_UTILITY,
    "ocr-benchmark-transcriber": EXEMPT_UTILITY,
    "browser-container-description": EXEMPT_UTILITY,
    "browser-scenario-generation": EXEMPT_UTILITY,
    "browser-full-scenario-generation": EXEMPT_UTILITY,
    "browser-atomic-test-generation": EXEMPT_UTILITY,
    "browser-email-generation": EXEMPT_UTILITY,
    "browser-entity-discovery": EXEMPT_UTILITY,
    "browser-relationship-resolution": EXEMPT_UTILITY,
    "browser-scan-all": EXEMPT_UTILITY,
    "template-designer": EXEMPT_UTILITY,
    "deck-importer": EXEMPT_UTILITY,
}

# A typo'd unit would silently count nothing: refuse it at import.
_unknown = sorted(d.unit for d in BUILTIN_ROI_UNITS.values() if d.unit not in BASELINES_BY_UNIT)
if _unknown:  # pragma: no cover - guarded by tests
    raise ValueError(f"unknown ROI unit(s) in BUILTIN_ROI_UNITS: {_unknown}")


def with_builtin_roi_unit(slug: str, output_schema: dict[str, Any] | None) -> dict[str, Any] | None:
    """*output_schema* with the agent's ``x-xtm-roi`` declaration, a copy.

    Unchanged (the same object) when the agent declares no unit or has no
    schema. A declaration already in the schema is replaced by the table's.
    """
    declaration = BUILTIN_ROI_UNITS.get(slug)
    if declaration is None or not isinstance(output_schema, dict):
        return output_schema
    schema = copy.deepcopy(output_schema)
    schema[ROI_SCHEMA_KEYWORD] = {"unit": declaration.unit, "count": declaration.count_path}
    return schema
