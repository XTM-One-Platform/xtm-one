"""Contract tests of the OpenCRQ tools against OpenCRQ's current tRPC API.

Each case drives one tool with ``_trpc`` patched and pins three things: the
procedure it calls, whether that call is a mutation, and the exact input it
sends. The input literals follow OpenCRQ's zod schemas, the file of each named
in a comment relative to ``back/src/features/app-router`` of the OpenCRQ
repository. The patched ``_trpc`` answers with a response recorded from a
running OpenCRQ and trimmed (``tests/data/opencrq``); the tool's output must
carry the recorded values and no placeholder where the response has one.

The write contracts already pinned in ``test_opencrq_package_tools.py`` (risk scenario
owner update, loss estimates, link creation to a primary asset, primary and supporting asset
creation, evidence, threat relevance, jobs listing, organization update) are not repeated.
"""

from __future__ import annotations

import contextlib
import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from app.integrations.http import format_http_error
from app.integrations.providers.opencrq import _common as opencrq_common
from app.integrations.providers.opencrq import admin as opencrq_admin
from app.integrations.providers.opencrq import assessments as opencrq_assessments
from app.integrations.providers.opencrq import assets as opencrq_assets
from app.integrations.providers.opencrq import build_tools
from app.integrations.providers.opencrq import crq as opencrq_crq
from app.integrations.providers.opencrq import frameworks as opencrq_frameworks
from app.integrations.providers.opencrq import mitre as opencrq_mitre
from app.integrations.providers.opencrq import ops as opencrq_ops
from app.integrations.providers.opencrq import risk as opencrq_risk
from app.integrations.providers.opencrq import treatment as opencrq_treatment
from app.tools.base import CallableToolExecutor, ToolCall

TOKEN = "https://demo.opencrq.io||test-token"
DATA = Path(__file__).parent / "data" / "opencrq"

TENANT = "89c70e9a-8317-4da7-8765-1b9ba004c32c"
ORGANIZATION = "90b57256-32e4-4369-b768-fa6f2bb64fee"
NIST_FRAMEWORK = "7e83ed4a-7d48-50e3-96b4-658c19a01804"
ISO_FRAMEWORK = "01a13a7c-19a9-7fb7-850c-e3fd65612f22"
DRATA_REQUIREMENT = "01a13a7c-19b6-70a6-b5c4-73465fd8f3e6"
ASSESSMENT = "019edb28-0034-7000-8000-000000000001"
ANSWERED_REQUIREMENT = "771c4c23-c370-536a-ad1a-0126ed3d6188"
UNANSWERED_REQUIREMENT = "aaaaaaaa-0000-4000-8000-000000000001"
ANSWER_OWNER = "612b238d-69ee-4c95-bf0a-f2b012d587e5"
CAPABILITY = "23c2d8f6-fe50-51ab-8b72-c527dd60be67"
SCENARIO = "019edb28-0011-7000-8000-000000000001"
ATTACK_OUTCOME = "fbe5a7bf-cb7b-5549-b147-8bd5d451c7b7"
CRQ_RUN = "01a13aaf-f0e7-70cc-b9f0-4953cc4a5801"
LOSS_DRIVER = "a59a5bd6-acb9-5b2f-bb5e-db69daa426b7"
SUPPORTING_ASSET = "019edb28-0038-7000-8000-000000000002"
UNNAMED_ASSET = "01a13a51-62c2-785c-bf01-33580d8e36e7"
PRIMARY_ASSET = "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d"
CORE_BANKING = "e5f6a7b8-c9d0-4e1f-aa3b-4c5d6e7f8a9b"
EVIDENCE = "01a13a4d-dca8-757a-84e9-c1216c518973"
CVE_RECORD = "7b1f0c52-4d7e-4c0a-9a53-0d1c2a9e5f10"
JOB = "01a13ab0-0bfb-74da-b3e3-2f187831218b"
PHISHING_TECHNIQUE = "1c75cd63-b80b-58a3-98c4-1f66b3ab6ad7"
MITIGATION = "0c47db65-3a58-5200-832c-411ff156e649"
# The first page of a scenario's walk, ranked by impact on the server.
EXPOSURE_PAGE = {"riskScenarioId": SCENARIO, "page": 1, "limit": 30, "sortBy": "impact", "sortDesc": True}


def _fixture(name: str) -> tuple[str, Any]:
    doc = json.loads((DATA / f"{name}.json").read_text(encoding="utf-8"))
    return doc["procedure"], doc["data"]


def _answers(names: tuple[str, ...], overrides: dict[str, Any]) -> dict[str, Any]:
    return {**dict(_fixture(name) for name in names), **overrides}


@contextlib.contextmanager
def _recorded_trpc(module: ModuleType, answers: dict[str, Any]):
    """Patch ``_trpc`` in *module* and in ``_common``; yield the recorded calls."""
    calls: list[dict] = []

    async def fake(
        base_url, headers, procedure, action, *, input=None, mutation=False,
        tenant_id="", client=None,
    ):
        calls.append(
            {"procedure": procedure, "input": input, "mutation": mutation, "tenant_id": tenant_id}
        )
        return answers.get(procedure), None

    with patch.object(module, "_trpc", fake), patch.object(opencrq_common, "_trpc", fake):
        yield calls


async def _execute(tool: str, /, **arguments):
    executor = CallableToolExecutor()
    build_tools(TOKEN, executor)
    return await executor.execute_tool(
        ToolCall(id="call-1", name=tool, arguments={"tenant_id": TENANT, **arguments})
    )


async def _run(tool: str, /, **arguments) -> str:
    result = await _execute(tool, **arguments)
    assert not result.is_error, result.content
    return result.content


def _tool_properties(name: str) -> dict:
    executor = CallableToolExecutor()
    tools = build_tools(TOKEN, executor)
    return next(t.input_schema["properties"] for t in tools if t.name == name)


def _link(path: str) -> str:
    """The "Open in OpenCRQ" link a formatter prints for *path* in the test tenant."""
    return f"Link: [Open in OpenCRQ](https://demo.opencrq.io/{TENANT}{path})"


def _tool_description(name: str) -> str:
    executor = CallableToolExecutor()
    tools = build_tools(TOKEN, executor)
    return next(t.description for t in tools if t.name == name)


def _sent(procedure: str, input: Any = None, *, mutation: bool = False) -> dict:
    return {"procedure": procedure, "input": input, "mutation": mutation, "tenant_id": TENANT}


def _assert_no_placeholder_for_a_value(out: str, answers: dict[str, Any]) -> None:
    """A placeholder in the output is only legitimate when the response itself carries that text."""
    recorded = json.dumps(answers, ensure_ascii=False)
    for placeholder in ("N/A", "None", "?"):
        if placeholder not in recorded:
            assert placeholder not in out, f"{placeholder!r} shown in:\n{out}"


# -- Reads ----------------------------------------------------------------------


@dataclass(frozen=True)
class Read:
    tool: str
    args: dict
    module: ModuleType
    calls: list[dict]
    fixtures: tuple[str, ...]
    expect: tuple[str, ...]
    absent: tuple[str, ...] = ()
    overrides: dict = field(default_factory=dict)


READS = {
    "list_opencrq_frameworks": Read(
        "list_opencrq_frameworks", {}, opencrq_frameworks,
        [
            # compliance-assessments/compliance-assessments-router.ts: getFrameworks, no input
            _sent("complianceAssessments.getFrameworks"),
            # drata/drata-router.ts: getFrameworks, no input
            _sent("drata.getFrameworks"),
        ],
        ("complianceAssessments.getFrameworks", "drata.getFrameworks"),
        (
            "(assessment catalog)", "NIST CSF 2.0", "Key: nist-csf-2.0", "Version: 1.2.0",
            "(Drata mirror)", "ISO 27001:2022", "Slug: iso27001-2022 (tag ISO270012022)",
            "Requirements (Drata report): 11 in scope, 6 ready",
            "Requirements (mirror): 12 total, 11 in scope, 6 ready", "In-scope controls: 9",
            "SOC 2", "Requirements (Drata report): 7 in scope, 4 ready",
        ),
    ),
    "get_opencrq_framework": Read(
        "get_opencrq_framework", {"framework_id": ISO_FRAMEWORK}, opencrq_frameworks,
        # drata/drata-router.ts: getFramework input is {id}
        [_sent("drata.getFramework", {"id": ISO_FRAMEWORK})],
        ("drata.getFramework",),
        (
            "ISO 27001:2022 (Drata mirror)", ISO_FRAMEWORK, _link(f"/compliance/{ISO_FRAMEWORK}"),
            "Slug: iso27001-2022",
            "Requirements (Drata report): 11 in scope, 6 ready", "In-scope controls: 9",
            "Last reported: 2026-10-14",
        ),
    ),
    "get_opencrq_framework_catalog_id": Read(
        "get_opencrq_framework", {"framework_id": NIST_FRAMEWORK}, opencrq_frameworks,
        [
            _sent("drata.getFramework", {"id": NIST_FRAMEWORK}),
            _sent("complianceAssessments.getFrameworks"),
        ],
        ("complianceAssessments.getFrameworks",),
        ("belongs to the assessment catalog", "list_opencrq_compliance_assessments", NIST_FRAMEWORK),
        overrides={"drata.getFramework": None},
    ),
    "list_opencrq_framework_requirements": Read(
        "list_opencrq_framework_requirements", {"framework_id": ISO_FRAMEWORK}, opencrq_frameworks,
        # drata/drata-router.ts: getRequirements input is {frameworkId}
        [_sent("drata.getRequirements", {"frameworkId": ISO_FRAMEWORK})],
        ("drata.getRequirements",),
        (
            "Requirements (3)",
            "4.1 Understanding the organization and its context [Clauses] in scope: yes, ready: no, controls: 1",
            "A.5.1 Policies for information security [Organizational] in scope: yes, ready: yes, controls: 1",
            "A.5.15 Access control", "controls: 2",
        ),
    ),
    "list_opencrq_drata_controls_by_framework": Read(
        "list_opencrq_drata_controls", {"framework_id": ISO_FRAMEWORK}, opencrq_frameworks,
        # drata/drata-router.ts: getControlsForFramework input is {frameworkId}
        [_sent("drata.getControlsForFramework", {"frameworkId": ISO_FRAMEWORK})],
        ("drata.getControlsForFramework",),
        (
            "Drata controls (3)",
            "DCF-1001 MFA enforced for every account: ready: yes, monitored: yes, evidence: yes, "
            "policy: no, requirements: 2",
            "DCF-1002 Information security policy approved", "DCF-1003 Security awareness training completed",
        ),
    ),
    "list_opencrq_drata_controls_by_requirement": Read(
        "list_opencrq_drata_controls", {"requirement_id": DRATA_REQUIREMENT}, opencrq_frameworks,
        # drata/drata-router.ts: getControlsForRequirement input is {requirementId}
        [_sent("drata.getControlsForRequirement", {"requirementId": DRATA_REQUIREMENT})],
        ("drata.getControlsForRequirement",),
        ("Drata controls (1)", "DCF-1004 Legacy VPN concentrator hardened", "archived 2026-07-16"),
    ),
    "list_opencrq_control_capabilities": Read(
        "list_opencrq_control_capabilities", {}, opencrq_frameworks,
        # controls/capability/control-capabilities-router.ts: getControlCapabilities, no input
        [_sent("controlCapabilities.getControlCapabilities")],
        ("controlCapabilities.getControlCapabilities",),
        (
            "Control capabilities (4):",
            "Cloud Security Posture Management (CSPM)",
            "Strength: L0 (auto); coverage: L1 (auto, assumed)",
            "Last assessed: 2026-10-14 by derivation",
            "Endpoint Hardening / Security Baseline (EHB)",
            "Strength: L1 (auto); coverage: L2 (auto, measured)",
            "Endpoint Detection and Response (EDR)",
            "Strength: L2 (manual); coverage: L2 (manual)",
            "Last assessed: 2026-10-10 by Platform Admin",
            "Multi-Factor Authentication (MFA)", "Strength: L3 (manual); coverage: L2 (manual)",
        ),
    ),
    "get_opencrq_control_capability": Read(
        "get_opencrq_control_capability", {"capability_id": CAPABILITY}, opencrq_frameworks,
        [
            # controls/capability/control-capabilities-router.ts: getControlCapability, getControlCapabilityRiskScenarios
            _sent("controlCapabilities.getControlCapability", {"id": CAPABILITY}),
            _sent("controlCapabilities.getControlCapabilityRiskScenarios", {"id": CAPABILITY}),
        ],
        (
            "controlCapabilities.getControlCapability",
            "controlCapabilities.getControlCapabilityRiskScenarios",
        ),
        (
            "Cloud Security Posture Management (CSPM)", _link(f"/controls/{CAPABILITY}/overview"),
            "Functions: detect, prevent",
            "Fed by: strength: drata; strength: NIST CSF 2.0; coverage: NIST CSF 2.0",
            "L0: No cloud posture management.", "L3: L2 plus pre-deployment infrastructure-as-code",
            "MITRE techniques, detect (1): T1040 Network Sniffing",
            "MITRE techniques, prevent (2): T1021 Remote Services, T1021.008 Direct Cloud VM Connections",
            "Linked risk scenarios (2)",
            "Business Interruption on Internet Banking Platform (organization Acme Bank Europe, "
            "shared techniques: 1",
            "Card Data Breach via Processing Partner (organization FinSecure Partners",
        ),
    ),
    "get_opencrq_control_capability_history": Read(
        "get_opencrq_control_capability_history", {"capability_id": CAPABILITY}, opencrq_frameworks,
        # controls/capability/control-capabilities-router.ts: getCapabilityEvents input is {capabilityId}
        [_sent("controlCapabilities.getCapabilityEvents", {"capabilityId": CAPABILITY})],
        ("controlCapabilities.getCapabilityEvents",),
        (
            "2026-10-14 09:00: assessment set: strength unset -> L2; coverage unset -> L1",
            "via NIST CSF 2.0 · T2 2026 by derivation",
        ),
    ),
    "list_opencrq_compliance_assessments": Read(
        "list_opencrq_compliance_assessments", {}, opencrq_assessments,
        # compliance-assessments/compliance-assessments-router.ts: getAssessments, no input
        [_sent("complianceAssessments.getAssessments")],
        ("complianceAssessments.getAssessments",),
        (
            "Compliance assessments (2) in tenant",
            "NIST CSF 2.0 · T4 2026", "Status: draft",
            "Answered on a level: 41 of 106 requirements, 2 not applicable",
            "Mean level (0-5): 2.05", "Requirements per level 0 to 5: 5, 7, 13, 13, 3, 0",
            "NIST CSF 2.0 · T2 2026", "Status: published",
            "Answered on a level: 99 of 106 requirements, 7 not applicable",
            "Framework: NIST CSF 2.0 (id: " + NIST_FRAMEWORK + ")",
        ),
    ),
    "get_opencrq_compliance_assessment_with_requirements": Read(
        "get_opencrq_compliance_assessment",
        {"assessment_id": ASSESSMENT, "include_requirements": True, "include_control_links": True},
        opencrq_assessments,
        # compliance-assessments/compliance-assessments-router.ts: getAssessment input is {id}
        [_sent("complianceAssessments.getAssessment", {"id": ASSESSMENT})],
        ("complianceAssessments.getAssessment",),
        (
            "NIST CSF 2.0 · T2 2026", "Status: published",
            "Created: 2026-06-16 by Alice Martin", "Published: 2026-07-16 by Alice Martin",
            "Requirements (4):",
            "GV.OC-01 The organizational mission is understood", "level 3 (owner Bob Johnson; due 2026-07-17)",
            "level 2 (owner Bob Johnson; due 2026-07-25)",
            "not applicable (owner Charlie Dupont; due 2026-08-30)",
            "Control links (3):", "PR.DS-10 -> API Security (API, axis capability",
            "PR.DS-10 -> Data Loss Prevention (DLP, axis capability",
        ),
    ),
    "get_opencrq_compliance_assessment": Read(
        "get_opencrq_compliance_assessment", {"assessment_id": ASSESSMENT}, opencrq_assessments,
        [_sent("complianceAssessments.getAssessment", {"id": ASSESSMENT})],
        ("complianceAssessments.getAssessment",),
        ("Answered on a level: 3 of 4 requirements, 1 not applicable", "Mean level (0-5): 3.00"),
        absent=("Requirements (", "Control links ("),
    ),
    "list_opencrq_risk_scenarios": Read(
        "list_opencrq_risk_scenarios", {"organization_id": ORGANIZATION}, opencrq_risk,
        # risk-scenarios/risk-scenarios-router.ts: getRiskScenariosOverview input is {organizationId?}
        [_sent("riskScenarios.getRiskScenariosOverview", {"organizationId": ORGANIZATION})],
        ("riskScenarios.getRiskScenariosOverview",),
        (
            "Risk scenarios (2, showing 2)", "Data Breach on Retail Lending Platform",
            "Latest run: ALE mean 18,670,571 (P10 9,109,408 / P90 30,389,564), LEF mean 0.48, "
            "39 of 39 techniques covered",
            "Readiness: ready", "Owner: Bob Johnson", "Primary assets: Credit Decision Process",
            "Data Breach / Exfiltration on Core Banking Database",
            "Latest run: ALE mean 2,111,063 (P10 418,540 / P90 4,202,588), LEF mean 0.0521",
        ),
    ),
    "list_opencrq_risk_scenarios_whole_tenant": Read(
        "list_opencrq_risk_scenarios", {}, opencrq_risk,
        [_sent("riskScenarios.getRiskScenariosOverview", {})],
        ("riskScenarios.getRiskScenariosOverview",),
        ("Risk scenarios (2, showing 2)",),
    ),
    "get_opencrq_risk_scenario": Read(
        "get_opencrq_risk_scenario", {"risk_scenario_id": SCENARIO}, opencrq_risk,
        [
            # risk-scenarios/risk-scenarios-router.ts: getRiskScenario {id}, getControlCapabilitiesForRiskScenario
            _sent("riskScenarios.getRiskScenario", {"id": SCENARIO}),
            _sent("riskScenarios.getControlCapabilitiesForRiskScenario", {"riskScenarioId": SCENARIO}),
        ],
        (
            "riskScenarios.getRiskScenario",
            "riskScenarios.getControlCapabilitiesForRiskScenario",
        ),
        (
            "Data Breach on Retail Lending Platform", "Attack outcome: Data Breach & Resale",
            "Business value categories: Services delivered to others under commitments, Personal data",
            "Mitigating control capabilities (3):",
            "API API Security (strength L2, coverage L1)",
            "EDR Endpoint Detection and Response (strength L2, coverage L2)",
            "EHB Endpoint Hardening / Security Baseline (strength L1, coverage L2)",
        ),
    ),
    "get_opencrq_scenario_exposure": Read(
        "get_opencrq_scenario_exposure", {"risk_scenario_id": SCENARIO}, opencrq_risk,
        # risk-scenarios/risk-scenarios-router.ts: listDerivedSupportingAssets input is
        # pagedInput(SCENARIO_SUPPORTING_ASSET_SORT_KEYS) + {riskScenarioId}
        [_sent("riskScenarios.listDerivedSupportingAssets", EXPOSURE_PAGE)],
        ("riskScenarios.listDerivedSupportingAssets",),
        (
            "Attack outcome: Data Breach & Resale (reaches confidentiality full)",
            "Derived supporting assets (2: 1 direct, 1 transitive, highest impact first):",
            "Core Banking System (Temenos T24) (via Credit Decision Process, depth 0, roles performs, impact 0.50)",
            "Next-Gen Firewall (Palo Alto) (via Core Banking System (Temenos T24), depth 1, impact 0.25)",
        ),
        absent=("more.",),
    ),
    "get_opencrq_scenario_exposure_past_one_page": Read(
        "get_opencrq_scenario_exposure", {"risk_scenario_id": SCENARIO}, opencrq_risk,
        [_sent("riskScenarios.listDerivedSupportingAssets", EXPOSURE_PAGE)],
        (),
        (
            "Derived supporting assets (71002: 12 direct, 70990 transitive, highest impact first):",
            "Core Banking System (Temenos T24) (via Credit Decision Process",
            "    ... and 71000 more.",
        ),
        overrides={
            "riskScenarios.listDerivedSupportingAssets": {
                **_fixture("riskScenarios.listDerivedSupportingAssets")[1],
                "total": 71002,
                "directCount": 12,
                "transitiveCount": 70990,
            }
        },
    ),
    "get_opencrq_scenario_exposure_without_counts": Read(
        "get_opencrq_scenario_exposure", {"risk_scenario_id": SCENARIO}, opencrq_risk,
        [_sent("riskScenarios.listDerivedSupportingAssets", EXPOSURE_PAGE)],
        (),
        ("Derived supporting assets (2, highest impact first):",),
        absent=("direct", "more."),
        overrides={
            "riskScenarios.listDerivedSupportingAssets": {
                **_fixture("riskScenarios.listDerivedSupportingAssets")[1],
                "total": "71002",
                "directCount": None,
                "transitiveCount": True,
            }
        },
    ),
    "get_opencrq_scenario_exposure_of_an_unknown_scenario": Read(
        "get_opencrq_scenario_exposure", {"risk_scenario_id": SCENARIO}, opencrq_risk,
        [_sent("riskScenarios.listDerivedSupportingAssets", EXPOSURE_PAGE)],
        (),
        ("Derived supporting assets: none",),
        absent=("Attack outcome",),
        # risk-scenarios-router.ts: ScenarioSupportingAssetsPageSchema.nullable()
        overrides={"riskScenarios.listDerivedSupportingAssets": None},
    ),
    "get_opencrq_cve_exploitation": Read(
        "get_opencrq_cve_exploitation",
        {"risk_scenario_id": SCENARIO, "cve_id": "cve-2024-6387"},
        opencrq_risk,
        # risk-scenarios/risk-scenarios-router.ts: getCveExploitation input is
        # {riskScenarioId, cveId (CVE-YYYY-NNNN), limit (1 to 500, default 50)}
        [
            _sent(
                "riskScenarios.getCveExploitation", {"riskScenarioId": SCENARIO, "cveId": "CVE-2024-6387", "limit": 30}
            )
        ],
        ("riskScenarios.getCveExploitation",),
        (
            "Exploitation on the scenario's assets (2 reached) for risk scenario",
            "CVE: CVE-2024-6387 - OpenSSH regreSSHion signal handler race (CVSS 8.1)",
            "internet-facing, 1 asset(s): T1190 Exploit Public-Facing Application (topology, medium confidence);"
            " topology rule, CVSS AV:N UI:N PR:N on internet-facing",
            "internal, 1 asset(s): T1210 Exploitation of Remote Services (topology, medium confidence);"
            " topology rule, CVSS AV:N UI:N PR:N on internal",
            "Edge bastion (OpenSSH) (internet-facing, impact 0.50, depth 1, primary assets Credit Decision Process):"
            " T1190",
            "Core Banking System (Temenos T24) (internal, impact 0.25, depth 0, primary assets Credit Decision Process):"
            " T1210",
        ),
        absent=("more.",),
    ),
    "get_opencrq_cve_exploitation_named_past_one_page": Read(
        "get_opencrq_cve_exploitation",
        {"risk_scenario_id": SCENARIO, "cve_id": "CVE-2024-6387"},
        opencrq_risk,
        [
            _sent(
                "riskScenarios.getCveExploitation", {"riskScenarioId": SCENARIO, "cveId": "CVE-2024-6387", "limit": 30}
            )
        ],
        (),
        (
            "(40 reached)",
            "mapping that names this CVE (curated)",
            "    ... and 38 more.",
        ),
        overrides={
            "riskScenarios.getCveExploitation": {
                **_fixture("riskScenarios.getCveExploitation")[1],
                "byExposure": [
                    {
                        **_fixture("riskScenarios.getCveExploitation")[1]["byExposure"][0],
                        "reading": {"kind": "named", "lanes": ["curated"]},
                    }
                ],
                "total": 40,
            }
        },
    ),
    "get_opencrq_attack_path_reach": Read(
        "get_opencrq_attack_path_reach",
        {"supporting_asset_id": SUPPORTING_ASSET},
        opencrq_risk,
        # supporting-assets/supporting-assets-router.ts: getAttackPathReach input is {supportingAssetId (uuid)}
        [_sent("supportingAssets.getAttackPathReach", {"supportingAssetId": SUPPORTING_ASSET})],
        ("supportingAssets.getAttackPathReach",),
        (
            "Attack-path reach (2 asset(s) reached, 2 hop(s), 1 proven, 1 primary asset(s) behind)",
            "Origin: Edge bastion (OpenSSH) (internet-facing, criticality high)",
            "Edge bastion (OpenSSH) -> Jump host (proven, simulation sim-2026-11-20",
            "Jump host -> Core Banking System (Temenos T24) (inferred, last reported",
            "Core Banking System (Temenos T24) (depth 2, internal, criticality very_high)",
            "Credit Decision Process (through Core Banking System (Temenos T24))",
            "Hops unconfirmed for 30 days are left out as stale.",
        ),
        absent=("more.", "none reached"),
    ),
    "get_opencrq_attack_path_reach_of_an_isolated_asset": Read(
        "get_opencrq_attack_path_reach",
        {"supporting_asset_id": SUPPORTING_ASSET},
        opencrq_risk,
        [_sent("supportingAssets.getAttackPathReach", {"supportingAssetId": SUPPORTING_ASSET})],
        (),
        (
            "Attack-path reach (0 asset(s) reached, 0 hop(s), 0 proven, 0 primary asset(s) behind)",
            "Hops: no attack path leaves this asset.",
            "Primary assets behind: none reached.",
        ),
        absent=("Reached assets:",),
        overrides={
            "supportingAssets.getAttackPathReach": {
                **_fixture("supportingAssets.getAttackPathReach")[1],
                "reachedAssets": [],
                "hops": [],
                "primaryAssets": [],
            }
        },
    ),
    "get_opencrq_attack_outcome_for_an_organization": Read(
        "get_opencrq_attack_outcome",
        {"attack_outcome_id": ATTACK_OUTCOME, "organization_id": ORGANIZATION}, opencrq_risk,
        [
            # attack-outcomes/attack-outcomes-router.ts: getAttackOutcome {id}, getMitreTechniquesForOutcome
            _sent("attackOutcomes.getAttackOutcome", {"id": ATTACK_OUTCOME}),
            _sent(
                "attackOutcomes.getMitreTechniquesForOutcome",
                {"attackOutcomeId": ATTACK_OUTCOME, "organizationId": ORGANIZATION},
            ),
        ],
        ("attackOutcomes.getAttackOutcome", "attackOutcomes.getMitreTechniquesForOutcome"),
        (
            "Data Breach & Resale", "Reaches: confidentiality full", "Reference dwell: 21 days",
            "MITRE techniques weighted for this organization (2, evidence: attacker-mixture)",
            "T1041 Exfiltration Over C2 Channel (signature, weight 0.18)",
            "T1048 Exfiltration Over Alternative Protocol (signature, weight 0.28)",
        ),
    ),
    "get_opencrq_attack_outcome_curated": Read(
        "get_opencrq_attack_outcome", {"attack_outcome_id": ATTACK_OUTCOME}, opencrq_risk,
        [
            _sent("attackOutcomes.getAttackOutcome", {"id": ATTACK_OUTCOME}),
            # attack-outcomes/attack-outcomes-router.ts: getCuratedMitreTechniquesForOutcome, no organization
            _sent("attackOutcomes.getCuratedMitreTechniquesForOutcome", {"attackOutcomeId": ATTACK_OUTCOME}),
        ],
        ("attackOutcomes.getAttackOutcome", "attackOutcomes.getCuratedMitreTechniquesForOutcome"),
        (
            "MITRE techniques curated on this outcome (3;",
            "T1041 Exfiltration Over C2 Channel (signature)",
            "T1567 Exfiltration Over Web Service (signature)",
        ),
    ),
    "get_opencrq_financial_summary": Read(
        "get_opencrq_financial_summary", {"risk_scenario_id": SCENARIO}, opencrq_risk,
        [
            # scenario-loss-estimates/scenario-loss-estimates-router.ts: getFinancialSummary, getScenarioLossEstimates
            _sent("scenarioLossEstimates.getFinancialSummary", {"riskScenarioId": SCENARIO}),
            _sent("scenarioLossEstimates.getScenarioLossEstimates", {"riskScenarioId": SCENARIO}),
        ],
        (
            "scenarioLossEstimates.getFinancialSummary",
            "scenarioLossEstimates.getScenarioLossEstimates",
        ),
        (
            "Total loss: min 8,282,415 / likely 29,225,370 / max 131,871,293, PERT expected 42,842,531",
            "Updated: 2026-10-14",
            "Loss estimates (2):",
            "Forensics & incident management: min 80,000 / likely 250,000 / max 600,000",
            "Regulatory fines: min 200,000 / likely 900,000 / max 3,500,000",
        ),
    ),
    "get_opencrq_crq_run_latest": Read(
        "get_opencrq_crq_run", {"risk_scenario_id": SCENARIO}, opencrq_crq,
        # crq-indicators/crq-indicators-router.ts: getLatestRun input is {riskScenarioId}
        [_sent("crqIndicators.getLatestRun", {"riskScenarioId": SCENARIO})],
        ("crqIndicators.getLatestRun",),
        (
            CRQ_RUN, "Iterations: 400",
            "Vulnerability: P5 0.117 / P50 0.136 / P95 0.229, mean 0.145",
            "LEF (loss event frequency): mean 0.48 (P10 0.262 / P90 0.7)",
            "ALE (annualized loss expectancy): mean 18,670,571 (P10 9,109,408 / P90 30,389,564)",
            "Loss of one successful attack: mean 42,839,724 (P10 28,510,137 / P90 57,834,363)",
            "TEF: min 1 / likely 3 / max 7",
            "blocked 85%, evicted 1%, impact noticed 6%, impact unnoticed 9%",
            "Techniques: 39 covered of 39",
        ),
        absent=("SLE",),
    ),
    "get_opencrq_crq_run_by_id": Read(
        "get_opencrq_crq_run", {"run_id": CRQ_RUN}, opencrq_crq,
        # crq-indicators/crq-indicators-router.ts: getRunWithScores input is {runId}
        [_sent("crqIndicators.getRunWithScores", {"runId": CRQ_RUN})],
        ("crqIndicators.getRunWithScores",),
        (
            "ALE (annualized loss expectancy): mean 18,670,571 (P10 9,109,408 / P90 30,389,564)",
            "Technique values (3;",
            "T1567 Exfiltration Over Web Service: prevent 1,989,018 / detect 1,084,472",
            "T1005 Data from Local System: prevent 2,038,223 / detect 432,454",
        ),
        absent=("SLE",),
    ),
    "list_opencrq_crq_run_history": Read(
        "list_opencrq_crq_run_history", {"risk_scenario_id": SCENARIO}, opencrq_crq,
        # crq-indicators/crq-indicators-router.ts: getRunHistory input is {riskScenarioId}
        [_sent("crqIndicators.getRunHistory", {"riskScenarioId": SCENARIO})],
        ("crqIndicators.getRunHistory",),
        (
            "CRQ run history (3):",
            "2026-10-14T13:52:51.431Z (id: " + CRQ_RUN + "): ALE mean 18,670,571, LEF mean 0.48",
            "2026-10-14T13:36:15.152Z (id: 01a13aa0-bd2f-7eba-8a92-e1996b46e1c4)",
        ),
    ),
    "get_opencrq_crq_what_if": Read(
        "get_opencrq_crq_what_if", {"risk_scenario_id": SCENARIO}, opencrq_crq,
        [
            # crq-indicators/crq-indicators-router.ts: getWhatIfResults input is {riskScenarioId}
            _sent("crqIndicators.getWhatIfResults", {"riskScenarioId": SCENARIO}),
            # controls/capability/control-capabilities-router.ts: getControlCapabilities, no input
            _sent("controlCapabilities.getControlCapabilities"),
        ],
        ("crqIndicators.getWhatIfResults", "controlCapabilities.getControlCapabilities"),
        (
            "Control upgrade what-if results (2, largest ALE reduction first):",
            "control d9d1969b-3111-50f8-8a4e-b491f2a7ec3b (coverage upgrade): "
            "strength L1 -> L1, coverage L1 -> L2: "
            "ALE reduction mean 4,019,389 (P10 2,387,738 / P90 6,981,223)",
            "control EDR Endpoint Detection and Response "
            "(id: 4c5d8d76-763f-5b20-8b5c-fe80d72682eb) (strength upgrade): "
            "strength L2 -> L3, coverage L2 -> L2: "
            "ALE reduction mean 0 (P10 0 / P90 0) [no gain: estate-families]",
        ),
    ),
    "list_opencrq_loss_drivers": Read(
        "list_opencrq_loss_drivers", {}, opencrq_crq,
        # loss-drivers/loss-drivers-router.ts: getLossDrivers takes no input
        [_sent("lossDrivers.getLossDrivers")],
        ("lossDrivers.getLossDrivers",),
        (
            "Loss drivers (3):",
            "Forensics & incident management (Response & Investigation, id: " + LOSS_DRIVER + ")",
            "Incident legal counsel (Response & Investigation",
            "Crisis communications & PR (Response & Investigation",
        ),
    ),
    "get_opencrq_loss_driver": Read(
        "get_opencrq_loss_driver", {"loss_driver_id": LOSS_DRIVER}, opencrq_crq,
        # loss-drivers/loss-drivers-router.ts: getLossDriver input is {id}
        [_sent("lossDrivers.getLossDriver", {"id": LOSS_DRIVER})],
        ("lossDrivers.getLossDriver",),
        (
            "Forensics & incident management", "Category: Response & Investigation",
            "Dwell sensitivity: strong", "Scales with: systems_in_scope",
            "Description: External DFIR fees",
        ),
    ),
    "search_opencrq_cves": Read(
        "search_opencrq_cves", {"query": "CVE-2026-9999"}, opencrq_assets,
        # cve/cve-router.ts: search input is {query (3..100), excludeAttachedToAssetId}
        [_sent("cve.search", {"query": "CVE-2026-9999"})],
        ("cve.search",),
        (
            "CVEs (1, showing 1)", "[CVE] CVE-2026-9999", "ID: 01a13a4b-1fce-7d6d-ad0b-839ee508f30f",
            _link("/settings/taxonomies/cve/CVE-2026-9999"),
        ),
    ),
    "list_opencrq_cves": Read(
        "list_opencrq_cves", {"limit": 5}, opencrq_assets,
        # cve/cve-router.ts: list input is pagedInput + search
        [_sent("cve.list", {"page": 1, "limit": 5})],
        ("cve.list",),
        (
            "CVEs (page 1, showing 2 of 16977)", "[CVE] CVE-2026-9999", "CVSS: 6.5",
            "[CVE] CVE-2026-9998", "CVSS: 9.8", "CISA KEV: no",
        ),
    ),
    "get_opencrq_cve": Read(
        "get_opencrq_cve", {"cve_id": "CVE-2026-9999"}, opencrq_assets,
        # cve/cve-router.ts: getInfo input is {cveId}
        [_sent("cve.getInfo", {"cveId": "CVE-2026-9999"})],
        ("cve.getInfo",),
        (
            "[CVE] CVE-2026-9999", _link("/settings/taxonomies/cve/CVE-2026-9999"), "CVSS: 6.5",
            "CISA KEV: no",
        ),
    ),
    "list_opencrq_supporting_assets": Read(
        "list_opencrq_supporting_assets", {"search": "#1 AI", "limit": 3}, opencrq_assets,
        # supporting-assets/supporting-assets-router.ts: listSupportingAssets, pagedInput + search
        [
            _sent(
                "supportingAssets.listSupportingAssets",
                {"page": 1, "limit": 3, "sortDesc": False, "search": "#1 AI"},
            )
        ],
        ("supportingAssets.listSupportingAssets",),
        (
            "Supporting assets (page 1, showing 2 of 2295)",
            "[SupportingAsset] #1 AI Meeting Note Taker - TeamsMaestro",
            "Category: identity", "Subcategory: service_account", "Criticality: low",
            "Last seen: 2026-10-12", "Sources: microsoft_graph",
            "[SupportingAsset] 172.16.1.192", "Category: generic_asset",
        ),
    ),
    "get_opencrq_supporting_asset": Read(
        "get_opencrq_supporting_asset", {"supporting_asset_id": SUPPORTING_ASSET}, opencrq_assets,
        # supporting-assets/supporting-assets-router.ts: getSupportingAsset input is {id}
        [_sent("supportingAssets.getSupportingAsset", {"id": SUPPORTING_ASSET})],
        ("supportingAssets.getSupportingAsset",),
        (
            "[SupportingAsset] ACME-SRV-0412", "Category: host", "Subcategory: server",
            "Criticality: high", "Exposure: internal", "Last seen: 2026-10-14",
            "Properties (machine): hostname acme-srv-0412, platform windows",
            "Applicable findings: 1",
        ),
    ),
    "get_opencrq_supporting_asset_unnamed": Read(
        "get_opencrq_supporting_asset", {"supporting_asset_id": UNNAMED_ASSET}, opencrq_assets,
        [_sent("supportingAssets.getSupportingAsset", {"id": UNNAMED_ASSET})],
        ("supportingAssets.getSupportingAsset.unnamed",),
        (
            "[SupportingAsset] 172.16.1.192", "Category: generic_asset", "Sources: microsoft_graph",
            "Applicable findings: 0",
        ),
    ),
    "get_opencrq_supporting_asset_shows_tags_url_and_linked_person": Read(
        "get_opencrq_supporting_asset", {"supporting_asset_id": SUPPORTING_ASSET}, opencrq_assets,
        # supporting-assets/service.ts getSupportingAsset: tags carry isManual, the recording has none
        [_sent("supportingAssets.getSupportingAsset", {"id": SUPPORTING_ASSET})],
        ("supportingAssets.getSupportingAsset",),
        (
            "URL: https://cmdb.acme.io/srv-0412", "Linked person: Jane Doe",
            "Tags: primary-asset (hand-set), edr",
        ),
        overrides={
            "supportingAssets.getSupportingAsset": {
                **_fixture("supportingAssets.getSupportingAsset")[1],
                "url": "https://cmdb.acme.io/srv-0412",
                "linkedPerson": "Jane Doe",
                "tags": [
                    {"id": "11111111-0000-4000-8000-000000000001", "name": "primary-asset", "isManual": True},
                    {"id": "11111111-0000-4000-8000-000000000002", "name": "edr", "isManual": False},
                ],
            },
        },
    ),
    "get_opencrq_evidence": Read(
        "get_opencrq_evidence", {"evidence_id": EVIDENCE}, opencrq_assets,
        # supporting-assets/supporting-assets-router.ts: getEvidenceDetail input is {evidenceId}
        [_sent("supportingAssets.getEvidenceDetail", {"evidenceId": EVIDENCE})],
        ("supportingAssets.getEvidenceDetail",),
        (
            "[AssetEvidence] id: " + EVIDENCE, "CVE: CVE-2026-9999",
            "Asset: workstation-01 (id: 01a13a4d-db9e-7183-9dc1-ebee96b0ffd8)",
            "Current state: applicable", "History (1):",
            "2026-10-14T10:47:58.622Z: applicable (Reported by Microsoft Defender) [origin: connector]",
            "Sources (1):",
            "microsoft_graph, connector 019edb28-0004-7000-8000-000000000004, last seen 2026-10-14T10:47:58.622Z",
        ),
    ),
    "list_opencrq_cve_evidences": Read(
        "list_opencrq_cve_evidences", {"cve_id": "CVE-2026-9999"}, opencrq_assets,
        # supporting-assets/supporting-assets-router.ts: getEvidencesForCve, pagedInput + cveId
        [
            _sent(
                "supportingAssets.getEvidencesForCve",
                {"cveId": "CVE-2026-9999", "page": 1, "limit": 25, "sortDesc": False},
            )
        ],
        ("supportingAssets.getEvidencesForCve",),
        (
            "Assets affected by CVE-2026-9999 (page 1, showing 2 of 6):",
            "workstation-01 (host, criticality unknown): applicable - Reported by Microsoft Defender "
            "(evidence id: 01a13a4b-2704-73ae-b9f5-0b8a3f24303b)",
            "workstation-02 (host, criticality unknown)",
        ),
    ),
    "list_opencrq_unique_findings": Read(
        "list_opencrq_unique_findings", {"limit": 2}, opencrq_assets,
        # finding-score/finding-score-router.ts: listUniqueFindings, pagedInput + estateInput + source
        [
            _sent(
                "findingScore.listUniqueFindings",
                {"page": 1, "limit": 2, "sortBy": "score", "sortDesc": True},
            )
        ],
        ("findingScore.listUniqueFindings",),
        (
            "Findings (page 1, showing 2 of 16985):",
            "CVE-2021-44228 Apache Log4j2 JNDI Remote Code Execution (Log4Shell) "
            "(medium 48, 3 asset(s), CISA KEV, EPSS 0.976, sources microsoft_graph, first seen 2025-11-13)",
            "CVE-2022-26134 Atlassian Confluence OGNL Injection Remote Code Execution (medium 46, 1 asset(s)",
        ),
    ),
    "list_opencrq_unique_findings_of_a_source": Read(
        "list_opencrq_unique_findings",
        {"page": 2, "limit": 2, "source": "microsoft_graph", "sort_by": "name", "sort_desc": False},
        opencrq_assets,
        [
            _sent(
                "findingScore.listUniqueFindings",
                {"page": 2, "limit": 2, "sortBy": "name", "sortDesc": False, "source": "microsoft_graph"},
            )
        ],
        ("findingScore.listUniqueFindings",),
        ("Findings (page 2, showing 2 of 16985):",),
    ),
    "list_opencrq_asset_dependencies": Read(
        "list_opencrq_asset_dependencies",
        {"supporting_asset_id": CORE_BANKING, "primary_asset_id": PRIMARY_ASSET}, opencrq_assets,
        [
            # asset-dependencies/asset-dependencies-router.ts: the three link reads
            _sent(
                "assetDependencies.getSupportToPrimaryLinksBySupportingAssetId",
                {"supportingAssetId": CORE_BANKING},
            ),
            _sent("assetDependencies.getSupportToSupportLinks", {"assetId": CORE_BANKING}),
            _sent(
                "assetDependencies.getSupportToPrimaryLinksByPrimaryAssetId",
                {"primaryAssetId": PRIMARY_ASSET},
            ),
        ],
        (
            "assetDependencies.getSupportToPrimaryLinksBySupportingAssetId",
            "assetDependencies.getSupportToSupportLinks",
            "assetDependencies.getSupportToPrimaryLinksByPrimaryAssetId",
        ),
        (
            "Support->primary (2):",
            "primary asset Customer Account Data (Acme Bank), roles stores, processes "
            "(primary asset id: " + PRIMARY_ASSET + ")",
            "primary asset Credit Decision Process (Acme Bank), roles performs",
            "Support->support (2):",
            "depends on Next-Gen Firewall (Palo Alto) (asset id: 15c5d6e7-f8a9-4b0c-ad2e-3f4a5b6c7d8e)",
            "provides to Primary Data Center (Paris)",
            "Primary asset links (2):",
            "supporting asset Core Banking System (Temenos T24) (host), roles stores, processes "
            "(supporting asset id: " + CORE_BANKING + ")",
            "supporting asset Internet Banking Portal (web_application), roles processes, reads",
        ),
    ),
    "list_opencrq_asset_dependencies_provenance_and_paging": Read(
        "list_opencrq_asset_dependencies",
        {"primary_asset_id": PRIMARY_ASSET, "offset": 1, "limit": 1},
        opencrq_assets,
        [
            _sent(
                "assetDependencies.getSupportToPrimaryLinksByPrimaryAssetId",
                {"primaryAssetId": PRIMARY_ASSET},
            ),
        ],
        (),
        (
            # asset-dependencies-router.ts LINK_PROVENANCE_OUTPUT: source, ruleId, ruleName on every link
            "Primary asset links (3, showing 2 to 2):",
            "supporting asset Internet Banking Portal (web_application), roles processes, reads, "
            "written by rule Web front ends support online banking",
            "... 1 more: call again with offset=2.",
        ),
        absent=("Core Banking System", "Payments Gateway"),
        overrides={
            "assetDependencies.getSupportToPrimaryLinksByPrimaryAssetId": [
                {
                    "id": "b4c5d6e7-f8a9-4b0c-9d2e-3f4a5b6c7d8e",
                    "supportingAssetId": CORE_BANKING,
                    "primaryAssetId": PRIMARY_ASSET,
                    "roles": ["stores"],
                    "source": "manual",
                    "ruleId": None,
                    "ruleName": None,
                    "supportingAssetName": "Core Banking System",
                    "supportingAssetDerivedName": None,
                    "supportingAssetCategoryId": "host",
                },
                {
                    "id": "41e1f2a3-b4c5-4d6e-9f8a-2b3c4d5e6f7a",
                    "supportingAssetId": "f6a7b8c9-d0e1-4f2a-ab4c-5d6e7f8a9b0c",
                    "primaryAssetId": PRIMARY_ASSET,
                    "roles": ["processes", "reads"],
                    "source": "rule",
                    "ruleId": "0c1d2e3f-4a5b-4c6d-8e7f-9a0b1c2d3e4f",
                    "ruleName": "Web front ends support online banking",
                    "supportingAssetName": "Internet Banking Portal",
                    "supportingAssetDerivedName": None,
                    "supportingAssetCategoryId": "web_application",
                },
                {
                    "id": "52f2a3b4-c5d6-4e7f-8a9b-3c4d5e6f7a8b",
                    "supportingAssetId": "a7b8c9d0-e1f2-4a3b-8c5d-6e7f8a9b0c1d",
                    "primaryAssetId": PRIMARY_ASSET,
                    "roles": ["processes"],
                    "source": "manual",
                    "ruleId": None,
                    "ruleName": None,
                    "supportingAssetName": "Payments Gateway",
                    "supportingAssetDerivedName": None,
                    "supportingAssetCategoryId": "web_application",
                },
            ],
        },
    ),
    "list_opencrq_dependency_rules": Read(
        "list_opencrq_dependency_rules",
        {},
        opencrq_assets,
        # asset-dependencies/asset-dependencies-router.ts: listDependencyRules, no input
        [_sent("assetDependencies.listDependencyRules")],
        (),
        (
            "Dependency rules (1):",
            "- Web front ends support online banking (id: 0c1d2e3f-4a5b-4c6d-8e7f-9a0b1c2d3e4f), enabled",
            "to primary asset Customer Account Data (primary asset id: " + PRIMARY_ASSET + "), roles processes, reads",
            "Conditions: category web_application and name glob *-portal and reported by microsoft_graph",
            "Links it holds now: 4",
        ),
        overrides={
            "assetDependencies.listDependencyRules": [
                {
                    "id": "0c1d2e3f-4a5b-4c6d-8e7f-9a0b1c2d3e4f",
                    "name": "Web front ends support online banking",
                    "primaryAssetId": PRIMARY_ASSET,
                    "primaryAssetName": "Customer Account Data",
                    "roles": ["processes", "reads"],
                    "enabled": True,
                    "categoryId": "web_application",
                    "subcategoryId": None,
                    "namePattern": "*-portal",
                    "namePatternKind": "glob",
                    "tagId": None,
                    "tagName": None,
                    "source": "microsoft_graph",
                    "linkCount": 4,
                },
            ],
        },
    ),
    "get_opencrq_job": Read(
        "get_opencrq_job", {"job_id": JOB}, opencrq_ops,
        # jobs/jobs-router.ts: getJob and getRun inputs are {id}
        [_sent("jobs.getJob", {"id": JOB}), _sent("jobs.getRun", {"id": JOB})],
        ("jobs.getJob", "jobs.getRun"),
        (
            "[Job] id: " + JOB,
            "Kind: crq-simulation",
            "Status: succeeded",
            "Enqueued: 2026-10-14T13:52:58.363Z",
            "Started: 2026-10-14T13:52:58.377Z",
            "Finished: 2026-10-14T13:53:41.902Z",
            "Progress: 12/12",
            "Attempt: 1/3",
            "Current step: simulated 12 of 12 scenarios",
            "Risk scenario ids: 5b0e7c3d-2f1a-4e9b-8c7d-6a5b4c3d2e1f, 9c8b7a6d-5e4f-4a3b-9c2d-1e0f9a8b7c6d",
        ),
    ),
    "get_opencrq_job_without_run_access": Read(
        "get_opencrq_job",
        {"job_id": JOB},
        opencrq_ops,
        # jobs.getRun answers nothing without MANAGE_SECURITY or platform access: status and times stay.
        [_sent("jobs.getJob", {"id": JOB}), _sent("jobs.getRun", {"id": JOB})],
        ("jobs.getJob",),
        (
            "[Job] id: " + JOB, "Status: succeeded",
            "Enqueued: 2026-10-14T13:52:58.363Z", "Started: 2026-10-14T13:52:58.377Z",
        ),
        absent=("Kind:", "Finished:", "Progress:", "Risk scenario ids:"),
    ),
    "list_opencrq_business_value_categories": Read(
        "list_opencrq_business_value_categories", {}, opencrq_assets,
        # business-value-categories/business-value-categories-router.ts: getBusinessValueCategories, no input
        [_sent("businessValueCategories.getBusinessValueCategories")],
        ("businessValueCategories.getBusinessValueCategories",),
        (
            "Business value categories (3):",
            "Operations and services (operations-and-services, id: 4574f84f-3d84-5c49-8ec1-149639f7b804)",
            "Services delivered to others under commitments (services-under-commitments",
            "Physical assets and facilities (physical-assets-and-facilities",
        ),
    ),
    "search_opencrq_mitre_techniques": Read(
        "search_opencrq_mitre_techniques", {"search": "phishing"}, opencrq_mitre,
        # mitre-attack/mitre-attack-router.ts: getTechniques takes no input
        [_sent("mitreAttack.getTechniques")],
        ("mitreAttack.getTechniques",),
        (
            "MITRE techniques (2, showing 2):",
            "T1566 Phishing (id: " + PHISHING_TECHNIQUE + ")",
            "T1566.001 Spearphishing Attachment (id: 7f90782d-cbf7-5326-b7bd-bc2e3defbf86) [sub-technique]",
        ),
        absent=("T1001",),
    ),
    "search_opencrq_mitre_techniques_without_subtechniques": Read(
        "search_opencrq_mitre_techniques", {"include_subtechniques": False}, opencrq_mitre,
        [_sent("mitreAttack.getTechniques")],
        ("mitreAttack.getTechniques",),
        (
            "MITRE techniques (2, showing 2):",
            "T1001 Data Obfuscation (id: 2974044c-d7ea-56d8-9b73-57b1fc294b7c)", "T1566 Phishing",
        ),
        absent=("[sub-technique]",),
    ),
    "get_opencrq_mitre_technique": Read(
        "get_opencrq_mitre_technique", {"technique_id": "T1566"}, opencrq_mitre,
        # mitre-attack/mitre-attack-router.ts: getTechniqueByExternalId input is {externalId}
        [_sent("mitreAttack.getTechniqueByExternalId", {"externalId": "T1566"})],
        ("mitreAttack.getTechniqueByExternalId",),
        (
            "[MitreTechnique] T1566 Phishing", "ID: " + PHISHING_TECHNIQUE,
            _link("/settings/taxonomies/mitre-attack/techniques/T1566/overview"),
            "Description: Adversaries may send phishing messages", "Tactics: TA0001 Initial Access",
            "Sub-techniques (2):", "T1566.001 Spearphishing Attachment", "Mitigations (6):",
            "M1017 User Training",
        ),
    ),
    "get_opencrq_mitre_technique_by_uuid": Read(
        "get_opencrq_mitre_technique", {"technique_id": PHISHING_TECHNIQUE}, opencrq_mitre,
        [
            _sent("mitreAttack.getTechniques"),
            _sent("mitreAttack.getTechniqueByExternalId", {"externalId": "T1566"}),
        ],
        ("mitreAttack.getTechniques", "mitreAttack.getTechniqueByExternalId"),
        ("[MitreTechnique] T1566 Phishing", "Sub-techniques (2):"),
    ),
    "list_opencrq_mitre_mitigations_detail": Read(
        "list_opencrq_mitre_mitigations", {"mitigation_id": "M1013"}, opencrq_mitre,
        # mitre-attack/mitre-attack-router.ts: getMitigationByExternalId input is {externalId}
        [_sent("mitreAttack.getMitigationByExternalId", {"externalId": "M1013"})],
        ("mitreAttack.getMitigationByExternalId",),
        (
            "[MitreMitigation] M1013 Application Developer Guidance",
            "ID: 0c47db65-3a58-5200-832c-411ff156e649",
            _link("/settings/taxonomies/mitre-attack/mitigations/M1013"),
            "Description: Application Developer Guidance focuses on",
            "Addresses techniques (3):", "T1078 Valid Accounts", "T1195 Supply Chain Compromise",
        ),
    ),
    "list_opencrq_mitre_mitigations_detail_by_uuid": Read(
        "list_opencrq_mitre_mitigations", {"mitigation_id": MITIGATION}, opencrq_mitre,
        [
            _sent("mitreAttack.getMitigations"),
            _sent("mitreAttack.getMitigationByExternalId", {"externalId": "M1013"}),
        ],
        ("mitreAttack.getMitigations", "mitreAttack.getMitigationByExternalId"),
        ("[MitreMitigation] M1013 Application Developer Guidance", "Addresses techniques (3):"),
    ),
}


@pytest.mark.parametrize("case", READS.values(), ids=list(READS))
async def test_read_tool_sends_the_current_input_and_shows_the_recorded_values(case: Read):
    answers = _answers(case.fixtures, case.overrides)
    with _recorded_trpc(case.module, answers) as calls:
        out = await _run(case.tool, **case.args)
    assert calls == case.calls
    for expected in case.expect:
        assert expected in out
    for unexpected in case.absent:
        assert unexpected not in out
    _assert_no_placeholder_for_a_value(out, answers)


# -- Writes ---------------------------------------------------------------------


@dataclass(frozen=True)
class Write:
    tool: str
    args: dict
    module: ModuleType
    procedure: str
    input: Any
    expect: tuple[str, ...]
    fixtures: tuple[str, ...] = ()
    overrides: dict = field(default_factory=dict)


NEW_ASSESSMENT = "01a13ab1-cf5c-77d0-a7a8-74a2b60f9edf"
PLAN_ID = "01a13ab2-1c3d-7e4f-8a5b-6c7d8e9f0a1b"
BUSINESS_VALUE_CATEGORIES = [
    "5ed6b543-76b5-546b-88e1-dc713a1a0f7e",
    "4e8336c9-87a8-5a6b-9089-2f279c89df79",
]

WRITES = {
    "create_opencrq_compliance_assessment": Write(
        "create_opencrq_compliance_assessment",
        {"framework_id": NIST_FRAMEWORK, "name": " NIST CSF 2.0 · T1 2027 "}, opencrq_assessments,
        # compliance-assessments/compliance-assessments-router.ts: createAssessment input is {name, frameworkId}
        "complianceAssessments.createAssessment",
        {"name": "NIST CSF 2.0 · T1 2027", "frameworkId": NIST_FRAMEWORK},
        ("Created compliance assessment 'NIST CSF 2.0 · T1 2027' as a draft (id: " + NEW_ASSESSMENT + ")",),
        fixtures=("complianceAssessments.createAssessment",),
    ),
    "manage_opencrq_compliance_assessment_rename": Write(
        "manage_opencrq_compliance_assessment",
        {"action": "rename", "assessment_id": ASSESSMENT, "name": "Renamed"}, opencrq_assessments,
        # compliance-assessments/compliance-assessments-router.ts: renameAssessment input is {id, name}
        "complianceAssessments.renameAssessment", {"id": ASSESSMENT, "name": "Renamed"},
        ("Renamed compliance assessment " + ASSESSMENT + " to 'Renamed'",),
    ),
    "manage_opencrq_compliance_assessment_publish": Write(
        "manage_opencrq_compliance_assessment",
        {"action": "publish", "assessment_id": ASSESSMENT}, opencrq_assessments,
        # compliance-assessments/compliance-assessments-router.ts: publishAssessment input is {id}
        "complianceAssessments.publishAssessment", {"id": ASSESSMENT},
        ("Published compliance assessment " + ASSESSMENT,),
    ),
    "manage_opencrq_compliance_assessment_reopen": Write(
        "manage_opencrq_compliance_assessment",
        {"action": "reopen", "assessment_id": ASSESSMENT}, opencrq_assessments,
        # compliance-assessments/compliance-assessments-router.ts: reopenAssessment input is {id}
        "complianceAssessments.reopenAssessment", {"id": ASSESSMENT},
        ("Reopened compliance assessment " + ASSESSMENT,),
    ),
    "update_opencrq_control_capability_assessment": Write(
        "update_opencrq_control_capability_assessment",
        {
            "capability_id": CAPABILITY, "strength": "l3", "coverage": "L1",
            "comment": " Reviewed with the SOC ",
        },
        opencrq_frameworks,
        # controls/capability/control-capabilities-router.ts: upsertAssessment (null leaves an axis as it is)
        "controlCapabilities.upsertAssessment",
        {
            "capabilityId": CAPABILITY, "strengthRung": "L3", "coverageBucket": "L1",
            "reattach": [], "comment": "Reviewed with the SOC",
        },
        (
            "Control capability " + CAPABILITY
            + ": declared strength L3, coverage L1, now manual.",
        ),
    ),
    "update_opencrq_control_capability_assessment_one_axis": Write(
        "update_opencrq_control_capability_assessment",
        {"capability_id": CAPABILITY, "coverage": "L3"}, opencrq_frameworks,
        "controlCapabilities.upsertAssessment",
        {
            "capabilityId": CAPABILITY, "strengthRung": None, "coverageBucket": "L3",
            "reattach": [], "comment": None,
        },
        ("Control capability " + CAPABILITY + ": declared coverage L3, now manual.",),
    ),
    "update_opencrq_control_capability_assessment_reattach": Write(
        "update_opencrq_control_capability_assessment",
        {"capability_id": CAPABILITY, "coverage": "L2", "reattach": ["Strength", "strength"]},
        opencrq_frameworks,
        # same file: reattach names the axes given back to their sources, never one declared
        "controlCapabilities.upsertAssessment",
        {
            "capabilityId": CAPABILITY, "strengthRung": None, "coverageBucket": "L2",
            "reattach": ["strength"], "comment": None,
        },
        (
            "Control capability " + CAPABILITY
            + ": declared coverage L2, now manual; re-attached strength to its sources.",
        ),
    ),
    "manage_opencrq_scope_control_posture_declare": Write(
        "manage_opencrq_scope_control_posture",
        {
            "action": "declare", "organization_id": ORGANIZATION, "capability_id": CAPABILITY,
            "strength": "l2", "coverage": " L1 ", "comment": " Hardened on this scope only ",
            "replace_overlay": True,
        },
        opencrq_frameworks,
        # controls/capability/control-capabilities-router.ts: declareScopeControlPosture replaces the whole
        # scope overlay, a null level follows the tenant
        "controlCapabilities.declareScopeControlPosture",
        {
            "organizationId": ORGANIZATION, "capabilityId": CAPABILITY, "strengthRung": "L2",
            "coverageBucket": "L1", "comment": "Hardened on this scope only",
        },
        (
            f"Organization {ORGANIZATION}, control capability {CAPABILITY}: "
            "the scope now overrides strength L2, coverage L1.",
        ),
    ),
    "manage_opencrq_scope_control_posture_declare_one_level": Write(
        "manage_opencrq_scope_control_posture",
        {
            "action": "declare", "organization_id": ORGANIZATION, "capability_id": CAPABILITY,
            "strength": "L3", "replace_overlay": True,
        },
        opencrq_frameworks,
        "controlCapabilities.declareScopeControlPosture",
        {
            "organizationId": ORGANIZATION, "capabilityId": CAPABILITY, "strengthRung": "L3",
            "coverageBucket": None, "comment": None,
        },
        ("the scope now overrides strength L3; coverage follows the tenant.",),
    ),
    "manage_opencrq_scope_control_posture_clear": Write(
        "manage_opencrq_scope_control_posture",
        {"action": "clear", "organization_id": ORGANIZATION, "capability_id": CAPABILITY, "dimension": "Coverage"},
        opencrq_frameworks,
        # same file: clearScopeControlPosture input is {organizationId, capabilityId, dimension}
        "controlCapabilities.clearScopeControlPosture",
        {"organizationId": ORGANIZATION, "capabilityId": CAPABILITY, "dimension": "coverage"},
        (
            f"Organization {ORGANIZATION}, control capability {CAPABILITY}: coverage follows the tenant again; "
            "the scope's other levels are unchanged",
        ),
    ),
    "manage_opencrq_capability_techniques_reset": Write(
        "manage_opencrq_capability_techniques",
        {"action": "reset", "capability_id": CAPABILITY, "function": " Detect "},
        opencrq_frameworks,
        # controls/capability/control-capabilities-router.ts: resetTechniqueOverrides input is {capabilityId, function}
        "controlCapabilities.resetTechniqueOverrides",
        {"capabilityId": CAPABILITY, "function": "detect"},
        (
            f"Control capability {CAPABILITY}: cleared this tenant's overrides on detect; "
            "the techniques it added are gone and the ones it removed are back",
        ),
    ),
    "update_opencrq_primary_asset": Write(
        "update_opencrq_primary_asset",
        {
            "primary_asset_id": PRIMARY_ASSET, "description": "Card payments",
            "business_value_category_ids": [BUSINESS_VALUE_CATEGORIES[0], None, f" {BUSINESS_VALUE_CATEGORIES[1]} "],
        },
        opencrq_assets,
        # primary-assets/primary-assets-router.ts: updatePrimaryAsset, businessValueCategoryIds (min 1)
        "primaryAssets.updatePrimaryAsset",
        {
            "id": PRIMARY_ASSET, "description": "Card payments",
            "businessValueCategoryIds": BUSINESS_VALUE_CATEGORIES,
        },
        ("Updated primary asset " + PRIMARY_ASSET,),
    ),
    "update_opencrq_primary_asset_clears_a_field": Write(
        "update_opencrq_primary_asset",
        {"primary_asset_id": PRIMARY_ASSET, "name": "Card payments", "description": "", "owner_user_id": ""},
        opencrq_assets,
        # primary-assets/primary-assets-router.ts: updatePrimaryAsset, description and ownerId are nullable, name is not
        "primaryAssets.updatePrimaryAsset",
        {"id": PRIMARY_ASSET, "name": "Card payments", "description": None, "ownerId": None},
        ("Updated primary asset " + PRIMARY_ASSET,),
    ),
    "update_opencrq_supporting_asset_sets_and_clears": Write(
        "update_opencrq_supporting_asset",
        {"supporting_asset_id": SUPPORTING_ASSET, "description": "Core ledger host", "owner_user_id": ""},
        opencrq_assets,
        # supporting-assets/supporting-assets-router.ts: updateSupportingAsset, description and ownerId are nullable
        "supportingAssets.updateSupportingAsset",
        {"id": SUPPORTING_ASSET, "description": "Core ledger host", "ownerId": None},
        ("Updated supporting asset " + SUPPORTING_ASSET,),
    ),
    "update_opencrq_supporting_asset_clears_the_description": Write(
        "update_opencrq_supporting_asset",
        {"supporting_asset_id": SUPPORTING_ASSET, "description": "", "owner_user_id": ANSWER_OWNER},
        opencrq_assets,
        # same file: an owner is a uuid, a description null removes it
        "supportingAssets.updateSupportingAsset",
        {"id": SUPPORTING_ASSET, "description": None, "ownerId": ANSWER_OWNER},
        ("Updated supporting asset " + SUPPORTING_ASSET,),
    ),
    "update_opencrq_supporting_asset_clears_url_and_subcategory": Write(
        "update_opencrq_supporting_asset",
        {"supporting_asset_id": SUPPORTING_ASSET, "url": "", "subcategory": "", "linked_person": "Jane Doe"},
        opencrq_assets,
        # supporting-assets/supporting-assets-router.ts: updateSupportingAsset, subcategoryId, url, linkedPerson are nullable
        "supportingAssets.updateSupportingAsset",
        {"id": SUPPORTING_ASSET, "subcategoryId": None, "url": None, "linkedPerson": "Jane Doe"},
        ("Updated supporting asset " + SUPPORTING_ASSET,),
    ),
    "update_opencrq_supporting_asset_sets_a_classification_tags_and_a_block": Write(
        "update_opencrq_supporting_asset",
        {
            "supporting_asset_id": SUPPORTING_ASSET, "category": "identity", "subcategory": " Service_Account ",
            "tag_names": [" primary-asset ", "", "edr"],
            "properties": {"user_principal_name": "svc@acme.io", "is_enabled": True, "privileged_pim_roles": []},
        },
        opencrq_assets,
        # same file: tagNames replaces the hand-set tags, properties is a family block in camelCase
        "supportingAssets.updateSupportingAsset",
        {
            "id": SUPPORTING_ASSET, "categoryId": "identity", "subcategoryId": "service_account",
            "tagNames": ["primary-asset", "edr"],
            "properties": {
                "family": "identity", "userPrincipalName": "svc@acme.io", "isEnabled": True,
                "privilegedPimRoles": [],
            },
        },
        ("Updated supporting asset " + SUPPORTING_ASSET,),
    ),
    "update_opencrq_supporting_asset_removes_every_hand_set_tag": Write(
        "update_opencrq_supporting_asset",
        {"supporting_asset_id": SUPPORTING_ASSET, "tag_names": []},
        opencrq_assets,
        # same file: an empty tagNames array is a value, and removes them all
        "supportingAssets.updateSupportingAsset",
        {"id": SUPPORTING_ASSET, "tagNames": []},
        ("Updated supporting asset " + SUPPORTING_ASSET,),
    ),
    "update_opencrq_asset_dependency_roles_replaces_the_roles_of_every_link": Write(
        "update_opencrq_asset_dependency_roles",
        {
            "link_ids": ["b4c5d6e7-f8a9-4b0c-9d2e-3f4a5b6c7d8e", "c5d6e7f8-a9b0-4c1d-ae3f-4a5b6c7d8e9f"],
            "roles": ["administers", "monitors"],
        },
        opencrq_assets,
        # asset-dependencies/asset-dependencies-router.ts: updateSupportToPrimaryLinks, an array of {id, roles}
        "assetDependencies.updateSupportToPrimaryLinks",
        [
            {"id": "b4c5d6e7-f8a9-4b0c-9d2e-3f4a5b6c7d8e", "roles": ["administers", "monitors"]},
            {"id": "c5d6e7f8-a9b0-4c1d-ae3f-4a5b6c7d8e9f", "roles": ["administers", "monitors"]},
        ],
        ("Updated 2 support_to_primary dependency link(s) to roles administers, monitors.",),
        overrides={"assetDependencies.updateSupportToPrimaryLinks": {"skippedIds": []}},
    ),
    "update_opencrq_risk_scenario_clears_the_description": Write(
        "update_opencrq_risk_scenario",
        {"risk_scenario_id": SCENARIO, "name": "Ransomware", "description": ""},
        opencrq_risk,
        # risk-scenarios/risk-scenarios-router.ts: updateRiskScenario (.strict()), description is nullable
        "riskScenarios.updateRiskScenario",
        {"id": SCENARIO, "name": "Ransomware", "description": None},
        ("Updated risk scenario " + SCENARIO,),
    ),
    "update_opencrq_risk_tolerance_sets_a_scenario_threshold": Write(
        "update_opencrq_risk_tolerance",
        {"risk_scenario_id": SCENARIO, "annual_loss": 2500000, "max_exceedance_probability": 0.05},
        opencrq_risk,
        # risk-tolerance.ts: RiskToleranceSchema; risk-scenarios/risk-scenarios-router.ts: setRiskTolerance
        "riskScenarios.setRiskTolerance",
        {"riskScenarioId": SCENARIO, "tolerance": {"annualLoss": 2500000, "maxExceedanceProbability": 0.05}},
        ("5% chance of losing more than 2,500,000 in a year", SCENARIO),
    ),
    "update_opencrq_risk_tolerance_sets_a_scope_threshold": Write(
        "update_opencrq_risk_tolerance",
        {"organization_id": ORGANIZATION, "annual_loss": 750000.5, "max_exceedance_probability": 0.001},
        opencrq_risk,
        # organizations/organizations-router.ts: setRiskTolerance, the tolerance is not nullable
        "organizations.setRiskTolerance",
        {"organizationId": ORGANIZATION, "tolerance": {"annualLoss": 750000.5, "maxExceedanceProbability": 0.001}},
        ("0.1% chance of losing more than 750,000 in a year", ORGANIZATION),
    ),
    "update_opencrq_risk_tolerance_clears_a_scenario_threshold": Write(
        "update_opencrq_risk_tolerance",
        {"risk_scenario_id": SCENARIO, "clear": True},
        opencrq_risk,
        # risk-scenarios/risk-scenarios-router.ts: setRiskTolerance accepts a null tolerance, the scenario follows its scope again
        "riskScenarios.setRiskTolerance",
        {"riskScenarioId": SCENARIO, "tolerance": None},
        ("follows its risk scope's tolerance again",),
    ),
    "update_opencrq_organization_sets_and_clears": Write(
        "update_opencrq_organization",
        {
            "organization_id": ORGANIZATION, "description": "Retail bank", "legal_entity_name": "",
            "domain": "", "sector_id": "", "country_id": "", "revenue_band": "", "employee_count": "",
        },
        opencrq_admin,
        # organizations/organizations-router.ts: OrganizationValuesSchema, each of these is nullable
        "organizations.updateOrganization",
        {
            "id": ORGANIZATION, "description": "Retail bank", "legalEntityName": None, "domain": None,
            "sectorId": None, "countryId": None, "revenueBand": None, "employeeCount": None,
        },
        ("Updated organization " + ORGANIZATION,),
    ),
    "update_opencrq_organization_sets_a_revenue_band_and_zero_employees": Write(
        "update_opencrq_organization",
        {"organization_id": ORGANIZATION, "revenue_band": " 1B_10B ", "employee_count": 0},
        opencrq_admin,
        # same file: employeeCount is a non-negative integer, so 0 is a value and not a removal
        "organizations.updateOrganization",
        {"id": ORGANIZATION, "revenueBand": "1b_10b", "employeeCount": 0},
        ("Updated organization " + ORGANIZATION,),
    ),
    "update_opencrq_organization_reads_a_decimal_string_as_the_employee_count": Write(
        "update_opencrq_organization",
        # the schema admits a string, so the arguments layer never coerces "12" to an integer
        {"organization_id": ORGANIZATION, "employee_count": " 12 "},
        opencrq_admin,
        "organizations.updateOrganization",
        {"id": ORGANIZATION, "employeeCount": 12},
        ("Updated organization " + ORGANIZATION,),
    ),
    "update_opencrq_organization_sets_the_reaction_time_the_estate_and_the_mapping": Write(
        "update_opencrq_organization",
        {
            "organization_id": ORGANIZATION, "reaction_time_min_hours": 0, "reaction_time_likely_hours": 4,
            "reaction_time_max_hours": 24.5, "estate_mode": " Closed ",
            "mapped_asset_ids_added": [SUPPORTING_ASSET, f" {SUPPORTING_ASSET} "],
            "mapped_asset_ids_removed": [CORE_BANKING],
        },
        opencrq_admin,
        # organizations/organizations-router.ts: OrganizationValuesSchema (hours, 0 is a valid minimum),
        # and mapping {added, removed} deduplicated by mappingIds
        "organizations.updateOrganization",
        {
            "id": ORGANIZATION, "reactionTimeMin": 0, "reactionTimeLikely": 4, "reactionTimeMax": 24.5,
            "estateMode": "closed", "mapping": {"added": [SUPPORTING_ASSET], "removed": [CORE_BANKING]},
        },
        ("Updated organization " + ORGANIZATION,),
    ),
    "update_opencrq_organization_clears_the_reaction_time_with_three_nulls": Write(
        "update_opencrq_organization",
        {"organization_id": ORGANIZATION, "clear_reaction_time": True},
        opencrq_admin,
        # same file: hasCoherentReactionTime accepts the three keys all null, never a subset
        "organizations.updateOrganization",
        {"id": ORGANIZATION, "reactionTimeMin": None, "reactionTimeLikely": None, "reactionTimeMax": None},
        ("Updated organization " + ORGANIZATION,),
    ),
    "update_opencrq_organization_unmaps_only": Write(
        "update_opencrq_organization",
        {"organization_id": ORGANIZATION, "mapped_asset_ids_removed": [CORE_BANKING]},
        opencrq_admin,
        # same file: updateOrganization defaults a missing side of mapping to []
        "organizations.updateOrganization",
        {"id": ORGANIZATION, "mapping": {"added": [], "removed": [CORE_BANKING]}},
        ("Updated organization " + ORGANIZATION,),
    ),
    "delete_opencrq_asset_evidence": Write(
        "delete_opencrq_asset_evidence", {"evidence_id": EVIDENCE}, opencrq_assets,
        # supporting-assets/supporting-assets-router.ts: deleteAssetEvidence input is {evidenceId}
        "supportingAssets.deleteAssetEvidence",
        {"evidenceId": EVIDENCE},
        ("Deleted evidence " + EVIDENCE,),
    ),
    # CTEM-18 (#1055): treatment-plans/treatment-plans-router.ts, TreatmentPlanCreateSchema and MovesSchema.
    "create_opencrq_treatment_plan_with_its_decision": Write(
        "create_opencrq_treatment_plan",
        {
            "organization_id": ORGANIZATION,
            "name": " Disable HTTP/2 now, migrate by January ",
            "owner_id": ANSWER_OWNER,
            "start_date": "2026-11-22",
            "end_date": "2027-01-31",
            "rationale": "CTEM case remediation decision",
            "cost_one_off": 6000,
            "cost_recurring_annual": 0,
            "moves": [
                {"capability_id": CAPABILITY, "target_strength": "l2"},
                {"capability_id": CAPABILITY, "target_strength": "L3", "target_coverage": "L2"},
            ],
        },
        opencrq_treatment,
        "treatmentPlans.createTreatmentPlan",
        {
            "name": "Disable HTTP/2 now, migrate by January",
            "rationale": "CTEM case remediation decision",
            "ownerId": ANSWER_OWNER,
            "startDate": "2026-11-22",
            "endDate": "2027-01-31",
            "costOneOff": 6000.0,
            "costRecurringAnnual": 0.0,
            "organizationId": ORGANIZATION,
            "moves": [{"capabilityId": CAPABILITY, "targetStrengthRung": "L3", "targetCoverageBucket": "L2"}],
        },
        ("Created treatment plan 'Disable HTTP/2 now, migrate by January' (id: " + PLAN_ID + ")", "1 control move(s)"),
        overrides={"treatmentPlans.createTreatmentPlan": PLAN_ID},
    ),
    "create_opencrq_treatment_plan_sends_only_what_it_was_given": Write(
        "create_opencrq_treatment_plan",
        {"organization_id": ORGANIZATION, "name": "Plan", "rationale": ""},
        opencrq_treatment,
        "treatmentPlans.createTreatmentPlan",
        {"name": "Plan", "organizationId": ORGANIZATION},
        ("Created treatment plan 'Plan' (id: " + PLAN_ID + ")",),
        overrides={"treatmentPlans.createTreatmentPlan": PLAN_ID},
    ),
    "update_opencrq_treatment_plan_sets_and_clears": Write(
        "update_opencrq_treatment_plan",
        {"treatment_plan_id": PLAN_ID, "end_date": "2027-02-28", "rationale": "", "owner_id": "", "cost_one_off": ""},
        opencrq_treatment,
        # treatment-plans-router.ts: updateTreatmentPlan {id, values: TreatmentPlanValuesSchema.partial()}
        "treatmentPlans.updateTreatmentPlan",
        {"id": PLAN_ID, "values": {"rationale": None, "ownerId": None, "endDate": "2027-02-28", "costOneOff": None}},
        ("Updated treatment plan " + PLAN_ID,),
    ),
    "set_opencrq_treatment_plan_moves_replaces_them": Write(
        "set_opencrq_treatment_plan_moves",
        {"treatment_plan_id": PLAN_ID, "moves": [{"capability_id": CAPABILITY, "target_coverage": "L3"}]},
        opencrq_treatment,
        "treatmentPlans.setTreatmentPlanScope",
        {
            "planId": PLAN_ID,
            "moves": [{"capabilityId": CAPABILITY, "targetStrengthRung": None, "targetCoverageBucket": "L3"}],
        },
        ("now plans 1 control move(s)", "coverage L3"),
    ),
    "set_opencrq_treatment_plan_moves_empties_them": Write(
        "set_opencrq_treatment_plan_moves",
        {"treatment_plan_id": PLAN_ID, "moves": []},
        opencrq_treatment,
        "treatmentPlans.setTreatmentPlanScope",
        {"planId": PLAN_ID, "moves": []},
        ("now plans no control move",),
    ),
    "add_opencrq_treatment_plan_moves_adds_them": Write(
        "add_opencrq_treatment_plan_moves",
        {"treatment_plan_id": PLAN_ID, "moves": [{"capability_id": CAPABILITY, "target_strength": "L1"}]},
        opencrq_treatment,
        "treatmentPlans.addTreatmentPlanMoves",
        {
            "planId": PLAN_ID,
            "moves": [{"capabilityId": CAPABILITY, "targetStrengthRung": "L1", "targetCoverageBucket": None}],
        },
        ("Added 1 control move(s) to treatment plan " + PLAN_ID,),
    ),
}


@pytest.mark.parametrize(
    ("tool", "args", "refusal"),
    [
        pytest.param("create_opencrq_treatment_plan", {"name": "P"}, "organization_id must be", id="no scope"),
        pytest.param(
            "create_opencrq_treatment_plan", {"organization_id": ORGANIZATION}, "name is required", id="no name"
        ),
        pytest.param(
            "create_opencrq_treatment_plan",
            {"organization_id": ORGANIZATION, "name": "P", "start_date": "2027-02-01", "end_date": "2027-01-01"},
            "end_date cannot be before start_date",
            id="inverted period",
        ),
        pytest.param(
            "create_opencrq_treatment_plan",
            {"organization_id": ORGANIZATION, "name": "P", "start_date": "2027-02-30"},
            "YYYY-MM-DD",
            id="no such date",
        ),
        pytest.param(
            "create_opencrq_treatment_plan",
            {"organization_id": ORGANIZATION, "name": "P", "cost_one_off": -1},
            "cost_one_off must be a number of at least 0",
            id="negative cost",
        ),
        pytest.param(
            "create_opencrq_treatment_plan",
            {"organization_id": ORGANIZATION, "name": "P", "owner_id": "sam"},
            "owner_id must be an OpenCRQ user UUID",
            id="owner by name",
        ),
        pytest.param(
            "update_opencrq_treatment_plan",
            {"treatment_plan_id": PLAN_ID},
            "at least one field",
            id="nothing to update",
        ),
        pytest.param(
            "update_opencrq_treatment_plan",
            {"treatment_plan_id": PLAN_ID, "name": " "},
            "name cannot be empty",
            id="empty name",
        ),
        pytest.param(
            "add_opencrq_treatment_plan_moves",
            {"treatment_plan_id": PLAN_ID, "moves": []},
            "at least one move",
            id="no move",
        ),
        pytest.param(
            "add_opencrq_treatment_plan_moves",
            {"treatment_plan_id": PLAN_ID, "moves": [{"capability_id": CAPABILITY}]},
            "give target_strength, target_coverage or both",
            id="move without target",
        ),
        pytest.param(
            "set_opencrq_treatment_plan_moves",
            {"treatment_plan_id": PLAN_ID, "moves": [{"capability_id": CAPABILITY, "target_strength": "L9"}]},
            "target_strength must be one of L0, L1, L2, L3",
            id="unknown rung",
        ),
        pytest.param(
            "set_opencrq_treatment_plan_moves",
            {"treatment_plan_id": "plan", "moves": []},
            "plan UUID",
            id="plan by name",
        ),
    ],
)
async def test_a_treatment_plan_write_that_cannot_be_sent_is_refused_before_any_call(
    tool: str, args: dict[str, Any], refusal: str
) -> None:
    with _recorded_trpc(opencrq_treatment, {}) as calls:
        out = await _run(tool, **args)
    assert calls == []
    assert refusal in out


async def test_a_treatment_plan_write_opencrq_refuses_is_reported_and_nothing_is_claimed() -> None:
    """The call carries the acting user's token: OpenCRQ's refusal (no write right) is the answer."""

    async def refused(*args: Any, **kwargs: Any) -> tuple[None, str]:
        return None, "Error: OpenCRQ refused to create treatment plan (403 FORBIDDEN)."

    with patch.object(opencrq_treatment, "_trpc", refused), patch.object(opencrq_common, "_trpc", refused):
        out = (await _execute("create_opencrq_treatment_plan", organization_id=ORGANIZATION, name="P")).content
    assert "403" in out
    assert "Created" not in out


@pytest.mark.parametrize("case", WRITES.values(), ids=list(WRITES))
async def test_write_tool_sends_the_current_input(case: Write):
    answers = _answers(case.fixtures, case.overrides)
    with _recorded_trpc(case.module, answers) as calls:
        out = await _run(case.tool, **case.args)
    assert calls == [_sent(case.procedure, case.input, mutation=True)]
    for expected in case.expect:
        assert expected in out


@pytest.mark.parametrize(
    ("args", "refusal"),
    [
        pytest.param({}, "exactly one of risk_scenario_id or organization_id", id="no target"),
        pytest.param(
            {"risk_scenario_id": SCENARIO, "organization_id": ORGANIZATION, "annual_loss": 1, "max_exceedance_probability": 0.1},
            "exactly one of risk_scenario_id or organization_id", id="two targets",
        ),
        pytest.param({"organization_id": ORGANIZATION, "clear": True}, "cannot be cleared", id="clearing a scope"),
        pytest.param(
            {"risk_scenario_id": SCENARIO, "clear": True, "annual_loss": 1}, "do not pass annual_loss", id="clear with a value",
        ),
        pytest.param({"risk_scenario_id": SCENARIO, "annual_loss": 1}, "both annual_loss", id="half a tolerance"),
        pytest.param(
            {"risk_scenario_id": SCENARIO, "annual_loss": 0, "max_exceedance_probability": 0.05},
            "annual_loss must be above zero", id="no loss",
        ),
        pytest.param(
            {"risk_scenario_id": SCENARIO, "annual_loss": 1000, "max_exceedance_probability": 5},
            "a fraction between 0.001 and 1 (5% is 0.05)", id="a percentage",
        ),
        pytest.param(
            {"risk_scenario_id": SCENARIO, "annual_loss": 1000, "max_exceedance_probability": 0.0005},
            "a fraction between 0.001 and 1", id="below the smallest probability",
        ),
    ],
)
async def test_a_risk_tolerance_that_cannot_be_sent_is_refused_before_any_call(args: dict, refusal: str):
    with _recorded_trpc(opencrq_risk, {}) as calls:
        out = await _run("update_opencrq_risk_tolerance", **args)
    assert calls == []
    assert refusal in out


async def test_a_capability_read_on_a_scope_says_which_axis_comes_from_the_scope():
    posture = {
        "capabilityId": CAPABILITY, "strengthRung": "L1", "coverageBucket": "L2",
        "strengthSource": "tenant", "coverageSource": "scope",
        "comment": "Pen-tested in Q3", "tenantStrengthRung": "L1", "tenantCoverageBucket": None,
        "scopeDeclaredAt": "2026-10-14T09:00:00.000Z",
    }
    answers = _answers(("controlCapabilities.getControlCapability",), {
        "controlCapabilities.getControlCapabilityRiskScenarios": [],
        "controlCapabilities.getResolvedControlPosture": [{**posture, "capabilityId": "other"}, posture],
    })
    with _recorded_trpc(opencrq_frameworks, answers) as calls:
        out = await _run("get_opencrq_control_capability", capability_id=CAPABILITY, organization_id=ORGANIZATION)
    # controls/capability/control-capabilities-router.ts: getResolvedControlPosture input is {organizationId}
    assert _sent("controlCapabilities.getResolvedControlPosture", {"organizationId": ORGANIZATION}) in calls
    for expected in (
        f"Posture on organization {ORGANIZATION}:",
        "Strength: L1 (tenant)",
        "Coverage: L2 (scope; tenant: not declared)",
        "Declared on the scope: 2026-10-14",
        "Scope comment: Pen-tested in Q3",
    ):
        assert expected in out


def _scope_posture(**axes: tuple[str | None, str]) -> dict:
    """A getResolvedControlPosture row: each axis is ``(level, source)``."""
    row: dict = {"capabilityId": CAPABILITY, "comment": "Pen-tested in Q3"}
    for axis, key in (("strength", "strengthRung"), ("coverage", "coverageBucket")):
        level, source = axes.get(axis, ("L1", "tenant"))
        row[key], row[f"{axis}Source"] = level, source
    return row


_DECLARE_POSTURE = "controlCapabilities.declareScopeControlPosture"
_READ_POSTURE = "controlCapabilities.getResolvedControlPosture"


async def _declare_posture(postures: Any, **args: Any) -> tuple[str, list[dict]]:
    with _recorded_trpc(opencrq_frameworks, {_READ_POSTURE: postures}) as calls:
        out = await _run(
            "manage_opencrq_scope_control_posture",
            action="declare", organization_id=ORGANIZATION, capability_id=CAPABILITY, **args,
        )
    return out, calls


async def test_declaring_a_posture_carries_over_the_axes_the_scope_already_sets():
    row = _scope_posture(strength=("L3", "scope"), coverage=("L2", "tenant"))
    out, calls = await _declare_posture([{**row, "capabilityId": "other"}, row], coverage="L0")
    assert calls == [
        _sent(_READ_POSTURE, {"organizationId": ORGANIZATION}),
        _sent(
            _DECLARE_POSTURE,
            {
                "organizationId": ORGANIZATION, "capabilityId": CAPABILITY, "strengthRung": "L3",
                "coverageBucket": "L0", "comment": None,
            },
            mutation=True,
        ),
    ]
    assert "the scope now overrides strength L3, coverage L0" in out
    assert "set: coverage L0" in out
    assert "kept from the scope: strength L3" in out
    assert "follow the tenant" not in out


async def test_declaring_a_posture_does_not_send_an_axis_the_tenant_provides():
    row = _scope_posture(strength=("L2", "tenant"), coverage=("L2", "tenant"))
    out, calls = await _declare_posture([row], strength="L3")
    assert calls[1]["input"] == {
        "organizationId": ORGANIZATION, "capabilityId": CAPABILITY, "strengthRung": "L3",
        "coverageBucket": None, "comment": None,
    }
    assert "kept from the scope" not in out
    assert "the scope now overrides strength L3; coverage follows the tenant." in out


async def test_declaring_a_posture_on_a_capability_without_a_resolved_row_sends_only_what_was_passed():
    out, calls = await _declare_posture([], strength="L3")
    assert calls[1]["input"]["strengthRung"] == "L3"
    assert calls[1]["input"]["coverageBucket"] is None
    assert "kept from the scope" not in out


async def test_declaring_a_posture_with_replace_overlay_reads_nothing_and_sends_only_what_was_passed():
    row = _scope_posture(strength=("L3", "scope"))
    out, calls = await _declare_posture([row], coverage="L2", replace_overlay=True)
    assert calls == [
        _sent(
            _DECLARE_POSTURE,
            {
                "organizationId": ORGANIZATION, "capabilityId": CAPABILITY, "strengthRung": None,
                "coverageBucket": "L2", "comment": None,
            },
            mutation=True,
        )
    ]
    assert "strength follows the tenant" in out


async def test_a_failed_posture_read_is_returned_and_nothing_is_written():
    async def fake(base_url, headers, procedure, action, **kwargs):
        calls.append(procedure)
        return None, "Error: OpenCRQ server error (503) while trying to get scope control posture."

    calls: list[str] = []
    with patch.object(opencrq_frameworks, "_trpc", fake), patch.object(opencrq_common, "_trpc", fake):
        result = await _execute(
            "manage_opencrq_scope_control_posture",
            action="declare", organization_id=ORGANIZATION, capability_id=CAPABILITY, strength="L1",
        )
    assert result.is_error
    assert "503" in result.content
    assert calls == [_READ_POSTURE]


def test_replace_overlay_is_a_boolean_argument_the_arguments_layer_coerces():
    assert _tool_properties("manage_opencrq_scope_control_posture")["replace_overlay"]["type"] == "boolean"


async def test_clearing_a_posture_refuses_replace_overlay():
    out = await _run(
        "manage_opencrq_scope_control_posture",
        action="clear", organization_id=ORGANIZATION, capability_id=CAPABILITY,
        dimension="strength", replace_overlay=True,
    )
    assert "do not pass replace_overlay" in out


async def test_a_capability_read_without_a_scope_does_not_ask_for_its_posture():
    answers = _answers(("controlCapabilities.getControlCapability",), {
        "controlCapabilities.getControlCapabilityRiskScenarios": [],
    })
    with _recorded_trpc(opencrq_frameworks, answers) as calls:
        out = await _run("get_opencrq_control_capability", capability_id=CAPABILITY)
    assert [c["procedure"] for c in calls] == [
        "controlCapabilities.getControlCapability",
        "controlCapabilities.getControlCapabilityRiskScenarios",
    ]
    assert "Posture on organization" not in out


REMOVED_TECHNIQUE = "0b9a5b3e-6a39-4f55-8a54-1f0c3d3a7e21"
ADDED_TECHNIQUE = "5c1f7d1a-2d3b-4b6e-9a77-0e2c8d4b6f10"
UNCOVERED_TECHNIQUE = "9d2e4f6a-1b3c-4d5e-8f70-a1b2c3d4e5f6"
CATALOGUE_TECHNIQUE = "414a3ce3-0099-59ec-b0ad-914aa2347012"


def _capability_with_overrides() -> dict:
    capability = _fixture("controlCapabilities.getControlCapability")[1]
    edge = {"function": "prevent", "tactics": []}
    return {
        **capability,
        "edges": [
            *capability["edges"],
            {**edge, "techniqueId": REMOVED_TECHNIQUE, "techniqueExternalId": "T1566", "techniqueName": "Phishing",
             "source": "tenant", "state": "removed"},
            {**edge, "techniqueId": ADDED_TECHNIQUE, "techniqueExternalId": "T1059", "techniqueName": "Command Interpreter",
             "source": "tenant", "state": "added"},
        ],
    }


@pytest.mark.parametrize(
    ("args", "procedure", "summary"),
    [
        pytest.param(
            {"action": "add", "technique_id": UNCOVERED_TECHNIQUE.upper()},
            "controlCapabilities.addTechniqueOverride",
            "now covers T1190 Exploit Public-Facing Application on prevent, added by this tenant.",
            id="an add",
        ),
        pytest.param(
            {"action": "remove", "technique_id": CATALOGUE_TECHNIQUE},
            "controlCapabilities.removeTechnique",
            "T1021 Remote Services is removed from prevent for this tenant; the catalogue still recommends it",
            id="removing a catalogue technique keeps it restorable",
        ),
        pytest.param(
            {"action": "remove", "technique_id": ADDED_TECHNIQUE},
            "controlCapabilities.removeTechnique",
            "T1059 Command Interpreter, which this tenant had added, is erased from prevent: it cannot be restored.",
            id="removing an addition erases it",
        ),
        pytest.param(
            {"action": "restore", "technique_id": REMOVED_TECHNIQUE},
            "controlCapabilities.restoreTechnique",
            "T1566 Phishing is back on prevent, as the catalogue recommends.",
            id="a restore",
        ),
    ],
)
async def test_a_capability_technique_is_changed_after_one_read_of_its_coverage(args: dict, procedure: str, summary: str):
    answers = {
        "controlCapabilities.getControlCapability": _capability_with_overrides(),
        "mitreAttack.getTechniques": [{"id": UNCOVERED_TECHNIQUE, "externalId": "T1190", "name": "Exploit Public-Facing Application"}],
    }
    with _recorded_trpc(opencrq_frameworks, answers) as calls:
        out = await _run("manage_opencrq_capability_techniques", capability_id=CAPABILITY, function="prevent", **args)
    assert calls == [
        # controls/capability/control-capabilities-router.ts: getControlCapability input is {id}
        _sent("controlCapabilities.getControlCapability", {"id": CAPABILITY}),
        # a technique the capability has no edge for is looked up in the catalogue before an add
        *([_sent("mitreAttack.getTechniques")] if args["action"] == "add" else []),
        # same file: addTechniqueOverride, removeTechnique and restoreTechnique take {capabilityId, techniqueId, function}
        _sent(
            procedure,
            {"capabilityId": CAPABILITY, "techniqueId": args["technique_id"].lower(), "function": "prevent"},
            mutation=True,
        ),
    ]
    assert summary in out


@pytest.mark.parametrize(
    ("action", "function", "technique", "refusal"),
    [
        pytest.param("add", "prevent", CATALOGUE_TECHNIQUE, "already covered for prevent (recommended by the catalogue)",
                     id="the catalogue covers it"),
        pytest.param("add", "prevent", ADDED_TECHNIQUE, "already covered for prevent (added by this tenant)",
                     id="an override exists"),
        pytest.param("add", "prevent", REMOVED_TECHNIQUE, "use action=restore", id="it was removed"),
        pytest.param("add", "detect", CATALOGUE_TECHNIQUE, None, id="another function is free"),
        pytest.param("remove", "prevent", UNCOVERED_TECHNIQUE, "not covered by this capability for prevent", id="not covered"),
        pytest.param("remove", "detect", CATALOGUE_TECHNIQUE, "not covered by this capability for detect", id="not covered on that function"),
        pytest.param("remove", "prevent", REMOVED_TECHNIQUE, "already removed from prevent", id="already removed"),
        pytest.param("restore", "prevent", CATALOGUE_TECHNIQUE, "only a catalogue technique this tenant removed", id="not removed"),
        pytest.param("restore", "prevent", ADDED_TECHNIQUE, "only a catalogue technique this tenant removed", id="an addition"),
        pytest.param("restore", "prevent", UNCOVERED_TECHNIQUE, "is not removed from prevent", id="unknown to the capability"),
    ],
)
async def test_a_technique_change_opencrq_would_refuse_is_refused_before_the_mutation(
    action: str, function: str, technique: str, refusal: str | None
):
    answers = {
        "controlCapabilities.getControlCapability": _capability_with_overrides(),
        "mitreAttack.getTechniques": [{"id": CATALOGUE_TECHNIQUE, "externalId": "T1021", "name": "Remote Services"}],
    }
    with _recorded_trpc(opencrq_frameworks, answers) as calls:
        out = await _run(
            "manage_opencrq_capability_techniques",
            action=action, capability_id=CAPABILITY, function=function, technique_id=technique,
        )
    if refusal is None:
        # the capability holds an edge for it on another function only: the catalogue is asked, then the add goes out
        assert [c["mutation"] for c in calls] == [False, False, True]
    else:
        assert [c["procedure"] for c in calls] == ["controlCapabilities.getControlCapability"]
        assert refusal in out


async def test_adding_a_technique_uuid_the_catalogue_does_not_hold_is_refused_before_the_mutation():
    answers = {
        "controlCapabilities.getControlCapability": _capability_with_overrides(),
        "mitreAttack.getTechniques": [{"id": CATALOGUE_TECHNIQUE, "externalId": "T1021", "name": "Remote Services"}],
    }
    with _recorded_trpc(opencrq_frameworks, answers) as calls:
        out = await _run(
            "manage_opencrq_capability_techniques",
            action="add", capability_id=CAPABILITY, function="detect", technique_id=UNCOVERED_TECHNIQUE,
        )
    assert out == f"MITRE technique {UNCOVERED_TECHNIQUE} not found."
    assert [c["mutation"] for c in calls] == [False, False]


async def test_a_capability_read_lists_what_the_tenant_changed_on_its_techniques():
    answers = {
        "controlCapabilities.getControlCapability": _capability_with_overrides(),
        "controlCapabilities.getControlCapabilityRiskScenarios": [],
    }
    with _recorded_trpc(opencrq_frameworks, answers):
        out = await _run("get_opencrq_control_capability", capability_id=CAPABILITY)
    assert "Tenant overrides (2): removed T1566 Phishing (prevent), added T1059 Command Interpreter (prevent)" in out


def test_the_technique_tool_says_a_service_account_is_refused_and_asks_for_confirmation():
    description = _tool_description("manage_opencrq_capability_techniques")
    assert "Needs a user session (a service account is refused)." in description
    assert "Confirm with the user" in description


ANSWER_OWNER_DUE_DATE = "2026-07-17"
ANSWER_UPDATED_AT = "2026-07-16T09:00:00.459Z"
NEW_ANSWER_OWNER = "5f0c2d1e-8a7b-4c3d-9e2f-1a0b9c8d7e6f"
ANSWER_UPDATED_AT = "2026-07-16T09:00:00.459Z"


@pytest.mark.parametrize(
    ("args", "requirement", "answer", "summary"),
    [
        pytest.param(
            {"level": 4}, ANSWERED_REQUIREMENT,
            {
                "ownerId": ANSWER_OWNER, "dueDate": ANSWER_OWNER_DUE_DATE, "level": 4,
                "notApplicable": False, "comment": None, "updatedAt": ANSWER_UPDATED_AT,
            },
            "as level 4.",
            id="a level keeps the owner and the due date",
        ),
        pytest.param(
            {"not_applicable": True}, ANSWERED_REQUIREMENT,
            {
                "ownerId": ANSWER_OWNER, "dueDate": ANSWER_OWNER_DUE_DATE, "level": None,
                "notApplicable": True, "comment": None, "updatedAt": ANSWER_UPDATED_AT,
            },
            "as not applicable.",
            id="not applicable clears the level",
        ),
        pytest.param(
            {"comment": "Evidence on the wiki"}, ANSWERED_REQUIREMENT,
            {
                "ownerId": ANSWER_OWNER, "dueDate": ANSWER_OWNER_DUE_DATE, "level": 3,
                "notApplicable": False, "comment": "Evidence on the wiki", "updatedAt": ANSWER_UPDATED_AT,
            },
            "as level 3, with a comment.",
            id="a comment alone keeps the level",
        ),
        pytest.param(
            {"clear": True}, ANSWERED_REQUIREMENT,
            {
                "ownerId": ANSWER_OWNER, "dueDate": ANSWER_OWNER_DUE_DATE, "level": None,
                "notApplicable": False, "comment": None, "updatedAt": ANSWER_UPDATED_AT,
            },
            "as not answered.",
            id="clear makes it unanswered and keeps the owner",
        ),
        pytest.param(
            {"owner_id": NEW_ANSWER_OWNER, "due_date": ""}, ANSWERED_REQUIREMENT,
            {
                "ownerId": NEW_ANSWER_OWNER, "dueDate": None, "level": 3,
                "notApplicable": False, "comment": None, "updatedAt": ANSWER_UPDATED_AT,
            },
            f"as level 3, owner {NEW_ANSWER_OWNER}, due date removed.",
            id="an owner and a due date are set or removed on their own",
        ),
        pytest.param(
            {"level": 0}, UNANSWERED_REQUIREMENT,
            {"ownerId": None, "dueDate": None, "level": 0, "notApplicable": False, "comment": None, "updatedAt": None},
            "as level 0.",
            id="a requirement without an answer has no owner to keep",
        ),
    ],
)
async def test_requirement_answer_is_the_full_upsert_with_what_was_read_back(
    args: dict, requirement: str, answer: dict, summary: str
):
    answers = _answers(("complianceAssessments.getAssessment",), {})
    with _recorded_trpc(opencrq_assessments, answers) as calls:
        out = await _run(
            "update_opencrq_requirement_answer",
            assessment_id=ASSESSMENT, requirement_id=requirement, **args,
        )
    assert calls == [
        # compliance-assessments/compliance-assessments-router.ts: getAssessment input is {id}
        _sent("complianceAssessments.getAssessment", {"id": ASSESSMENT}),
        # same file: answerRequirement requires every one of its keys, nullable or not; updatedAt is the version read
        _sent(
            "complianceAssessments.answerRequirement",
            {"assessmentId": ASSESSMENT, "requirementId": requirement, **answer},
            mutation=True,
        ),
    ]
    assert out.endswith(summary)


def _answer_requirement_error(status: int, error: dict):
    """Serve the assessment read, then answer the ``answerRequirement`` mutation with a tRPC error envelope."""
    _, assessment = _fixture("complianceAssessments.getAssessment")

    async def answer(self: httpx.AsyncClient, method: str, url: str, **kwargs: Any) -> httpx.Response:
        if url.endswith("/complianceAssessments.getAssessment"):
            return httpx.Response(
                200, json={"result": {"data": assessment}}, request=httpx.Request("GET", url)
            )
        return httpx.Response(status, json={"error": error}, request=httpx.Request("POST", url))

    return patch.object(httpx.AsyncClient, "request", answer)


def _conflict(reason: str | None, message: str = "Refused") -> dict:
    """A tRPC CONFLICT envelope as OpenCRQ serialises it: the machine code sits in ``data.reason``."""
    return {
        "message": message,
        "code": -32009,
        "data": {"code": "CONFLICT", "httpStatus": 409, "reason": reason},
    }


async def test_a_requirement_answer_changed_since_it_was_read_is_not_saved_and_says_to_read_it_again():
    with _answer_requirement_error(409, _conflict("STALE_WRITE", "Any wording at all")):
        result = await _execute(
            "update_opencrq_requirement_answer",
            assessment_id=ASSESSMENT, requirement_id=ANSWERED_REQUIREMENT, level=4,
        )
    assert result.content.startswith("Error: someone changed requirement")
    assert "nothing was saved" in result.content
    assert "get_opencrq_compliance_assessment with include_requirements" in result.content
    assert "confirmation" in result.content


async def test_a_requirement_answer_on_a_published_assessment_says_to_reopen_it():
    with _answer_requirement_error(409, _conflict("ASSESSMENT_PUBLISHED", "Any wording at all")):
        result = await _execute(
            "update_opencrq_requirement_answer",
            assessment_id=ASSESSMENT, requirement_id=ANSWERED_REQUIREMENT, level=4,
        )
    assert result.content.startswith(f"Error: compliance assessment {ASSESSMENT} is published")
    assert "nothing was saved" in result.content
    assert "manage_opencrq_compliance_assessment (action=reopen)" in result.content


@pytest.mark.parametrize(
    ("status", "error"),
    [
        pytest.param(409, _conflict("SOME_OTHER_CODE", "Refused for another reason"), id="another code"),
        pytest.param(
            409,
            _conflict(None, "This answer has changed since it was last read"),
            id="the stale wording without the code is not a stale write",
        ),
        pytest.param(
            400,
            {"message": "Invalid input", "code": -32600, "data": {"code": "BAD_REQUEST", "httpStatus": 400}},
            id="a bad request is passed through",
        ),
    ],
)
async def test_another_requirement_answer_error_is_passed_through_unchanged(status: int, error: dict):
    with _answer_requirement_error(status, error):
        result = await _execute(
            "update_opencrq_requirement_answer",
            assessment_id=ASSESSMENT, requirement_id=ANSWERED_REQUIREMENT, level=4,
        )
    assert result.content.startswith("Error: OpenCRQ ")
    assert error["message"] in result.content
    assert "someone changed" not in result.content


def _loss_estimates_upsert_error(status: int, error: dict):
    """Serve an empty estimates read, then answer the upsert with a tRPC error envelope."""

    async def answer(self: httpx.AsyncClient, method: str, url: str, **kwargs: Any) -> httpx.Response:
        if url.endswith("/scenarioLossEstimates.getScenarioLossEstimates"):
            return httpx.Response(200, json={"result": {"data": []}}, request=httpx.Request("GET", url))
        return httpx.Response(status, json={"error": error}, request=httpx.Request("POST", url))

    return patch.object(httpx.AsyncClient, "request", answer)


async def _update_loss_estimates():
    return await _execute(
        "update_opencrq_loss_estimates",
        risk_scenario_id=SCENARIO,
        estimates=[{"loss_driver_id": LOSS_DRIVER, "min_value": 1, "likely_value": 2, "max_value": 3}],
    )


async def test_loss_estimates_changed_since_they_were_read_are_not_saved_and_say_to_read_them_again():
    with _loss_estimates_upsert_error(409, _conflict("STALE_WRITE", "Any wording at all")):
        result = await _update_loss_estimates()
    assert result.content.startswith(f"Error: the loss estimates of risk scenario {SCENARIO} changed")
    assert "nothing was saved" in result.content
    assert "get_opencrq_financial_summary" in result.content
    assert "confirmation" in result.content


@pytest.mark.parametrize(
    ("status", "error"),
    [
        pytest.param(409, _conflict(None, "Some other conflict"), id="a conflict without a code"),
        pytest.param(409, _conflict("ASSESSMENT_PUBLISHED", "Some other conflict"), id="another code"),
        pytest.param(
            400,
            {"message": "Invalid input", "code": -32600, "data": {"code": "BAD_REQUEST", "httpStatus": 400}},
            id="a bad request",
        ),
    ],
)
async def test_another_loss_estimates_error_is_passed_through_unchanged(status: int, error: dict):
    with _loss_estimates_upsert_error(status, error):
        result = await _update_loss_estimates()
    assert result.content.startswith("Error: OpenCRQ ")
    assert error["message"] in result.content
    assert "changed since they were read" not in result.content


def _envelope_client(status: int, body: Any = None, *, content: bytes | None = None):
    """A client whose transport answers every request with *status* and a tRPC body (or raw *content*)."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if content is not None:
            return httpx.Response(status, content=content)
        return httpx.Response(status, json=body)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), seen


async def _call_trpc(client: httpx.AsyncClient):
    async with client:
        return await opencrq_common._trpc(
            "https://demo.opencrq.io", {"Authorization": "Bearer t"},
            "complianceAssessments.answerRequirement", "answer a requirement",
            input={"assessmentId": ASSESSMENT}, mutation=True, tenant_id=TENANT, client=client,
        )


def _http_error_text(status: int, **response: Any) -> str:
    request = httpx.Request("POST", "https://demo.opencrq.io/trpc/complianceAssessments.answerRequirement")
    return format_http_error(
        httpx.Response(status, request=request, **response), "OpenCRQ", "answer a requirement"
    )


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]


async def test_a_trpc_409_stale_write_carries_its_reason_and_status_and_is_still_logged(
    caplog: pytest.LogCaptureFixture,
):
    error = _conflict("STALE_WRITE", "This answer has changed since it was last read")
    client, seen = _envelope_client(409, {"error": error})
    with caplog.at_level("WARNING"):
        data, err = await _call_trpc(client)
    assert data is None
    assert isinstance(err, opencrq_common.TrpcError)
    assert err.reason == "STALE_WRITE"
    assert err.status == 409
    assert err == _http_error_text(409, json={"error": error})
    assert err.startswith("Error: ")
    assert [r.method for r in seen] == ["POST"]
    assert any("HTTP error" in m and "409" in m for m in _warnings(caplog))


@pytest.mark.parametrize(
    ("status", "body", "content"),
    [
        pytest.param(409, {"error": _conflict(None)}, None, id="a null code"),
        pytest.param(
            400, {"error": {"message": "Invalid input", "data": {"code": "BAD_REQUEST"}}}, None,
            id="no reason key",
        ),
        pytest.param(409, {"error": {"message": "Refused", "data": {"reason": 5}}}, None, id="a non-string code"),
        pytest.param(422, None, b"not json at all", id="a body that is not JSON"),
        pytest.param(404, ["unexpected"], None, id="a body that is not an object"),
    ],
)
async def test_a_trpc_4xx_without_a_reason_keeps_its_error_text_and_logs(
    status: int, body: Any, content: bytes | None, caplog: pytest.LogCaptureFixture
):
    client, _ = _envelope_client(status, body, content=content)
    with caplog.at_level("WARNING"):
        data, err = await _call_trpc(client)
    assert data is None
    assert isinstance(err, opencrq_common.TrpcError)
    assert err.reason is None
    assert err.status == status
    assert err == _http_error_text(status, **({"content": content} if content is not None else {"json": body}))
    assert any("HTTP error" in m for m in _warnings(caplog))


async def test_a_trpc_5xx_envelope_keeps_its_reason_and_status(caplog: pytest.LogCaptureFixture):
    client, _ = _envelope_client(
        500, {"error": {"message": "Internal error", "data": {"reason": "SOME_CODE"}}}
    )
    with caplog.at_level("WARNING"):
        _, err = await _call_trpc(client)
    assert err.status == 500
    assert err.reason == "SOME_CODE"
    assert any("HTTP error" in m or "500" in m for m in _warnings(caplog))


async def test_a_proxy_5xx_page_has_a_status_and_no_reason():
    client, _ = _envelope_client(502, content=b"<html>Bad gateway</html>")
    _, err = await _call_trpc(client)
    assert err.status == 502
    assert err.reason is None


async def test_a_trpc_network_error_is_a_plain_error_without_status_or_reason():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    _, err = await _call_trpc(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert err.startswith("Error: ")
    assert getattr(err, "reason", None) is None
    assert getattr(err, "status", None) is None


async def test_a_trpc_200_with_an_error_body_carries_its_reason_and_status():
    client, _ = _envelope_client(200, {"error": _conflict("STALE_WRITE", "Stale")})
    _, err = await _call_trpc(client)
    assert err == "Error: OpenCRQ answer a requirement failed: Stale"
    assert err.reason == "STALE_WRITE"
    assert err.status == 200


async def test_a_trpc_200_returns_its_data_with_no_error():
    client, seen = _envelope_client(200, {"result": {"data": {"ok": True}}})
    data, err = await _call_trpc(client)
    assert data == {"ok": True}
    assert err is None
    assert seen[0].headers["x-tenant-id"] == TENANT


async def test_risk_scenario_creation_sends_exactly_the_keys_opencrq_accepts():
    with _recorded_trpc(opencrq_risk, {}) as calls:
        out = await _run(
            "create_opencrq_risk_scenario",
            organization_id=ORGANIZATION, name="Ransomware", description="Encrypts the estate",
            attack_outcome_id=ATTACK_OUTCOME, primary_asset_ids=[PRIMARY_ASSET],
        )
    (call,) = calls
    scenario_id = call["input"]["id"]
    assert uuid.UUID(scenario_id)
    assert call == _sent(
        "riskScenarios.createRiskScenario",
        # risk-scenarios/risk-scenarios-router.ts: createRiskScenario, with no confidence
        {
            "id": scenario_id, "organizationId": ORGANIZATION, "name": "Ransomware",
            "description": "Encrypts the estate", "attackOutcomeId": ATTACK_OUTCOME,
            "primaryAssetIds": [PRIMARY_ASSET],
        },
        mutation=True,
    )
    assert f"(id: {scenario_id})" in out


async def test_organization_creation_sends_an_array_of_one_scope_with_its_mapping():
    with _recorded_trpc(opencrq_admin, {}) as calls:
        out = await _run(
            "create_opencrq_organization",
            name="Retail", reaction_time_min_hours=1, reaction_time_likely_hours=4, reaction_time_max_hours=12,
            estate_mode="closed", mapped_asset_ids=[SUPPORTING_ASSET, SUPPORTING_ASSET, CORE_BANKING],
        )
    (call,) = calls
    (scope,) = call["input"]
    assert uuid.UUID(scope["id"])
    assert call == _sent(
        "organizations.createOrganizationsTransaction",
        # organizations/organizations-router.ts: CreateOrganizationInputSchema, an array whose items carry mappedAssetIds
        [{
            "id": scope["id"], "name": "Retail", "reactionTimeMin": 1, "reactionTimeLikely": 4,
            "reactionTimeMax": 12, "estateMode": "closed", "mappedAssetIds": [SUPPORTING_ASSET, CORE_BANKING],
        }],
        mutation=True,
    )
    assert f"(id: {scope['id']})" in out


@pytest.mark.parametrize("tool", ["delete_opencrq_asset_evidence", "reset_opencrq_evidence_state"])
def test_an_evidence_action_says_a_service_account_is_refused(tool: str):
    assert "Needs a user session (a service account is refused)." in _tool_description(tool)


def test_resetting_an_evidence_states_what_it_needs_and_what_it_risks():
    description = _tool_description("reset_opencrq_evidence_state")
    for expected in ("live connector", "set by a person", "override the state a person set", "get_opencrq_evidence"):
        assert expected in description


def test_deleting_an_evidence_says_a_connector_s_evidence_is_refused():
    description = _tool_description("delete_opencrq_asset_evidence")
    assert "attached by hand" in description
    assert "a connector reported is refused" in description


async def test_creating_an_evidence_answers_with_the_id_of_the_evidence_not_the_cve():
    created = {"id": EVIDENCE, "supportingAssetId": SUPPORTING_ASSET, "cveId": CVE_RECORD}
    with _recorded_trpc(opencrq_assets, {"supportingAssets.createAssetEvidence": created}) as calls:
        out = await _run("create_opencrq_asset_evidence", supporting_asset_id=SUPPORTING_ASSET, cve_record_id=CVE_RECORD)
    # supporting-assets/supporting-assets-router.ts: createAssetEvidence input is {supportingAssetId, cveId}
    assert calls == [_sent("supportingAssets.createAssetEvidence", {"supportingAssetId": SUPPORTING_ASSET, "cveId": CVE_RECORD}, mutation=True)]
    assert f"evidence id: {EVIDENCE}" in out
    assert CVE_RECORD in out


def _evidence_detail(*, sources: list[dict] | None = None, history: list[dict] | None = None) -> dict:
    detail = _fixture("supportingAssets.getEvidenceDetail")[1]
    return {
        **detail,
        "sources": detail["sources"] if sources is None else sources,
        "history": detail["history"] if history is None else history,
    }


# OpenCRQ serves the history oldest first and resetEvidenceState checks the row of the
# latest stateChangedAt, the larger id breaking a tie: the verdict must not hang on the order.
_SET_ASIDE = _fixture("supportingAssets.getEvidenceDetail.set-by-a-person")[1]
_REPORTED, _SET_BY_A_PERSON = _SET_ASIDE["history"]
_AFTERWARDS = "2026-10-14T15:30:00.000Z"
_REPORTED_AFTERWARDS = {**_REPORTED, "id": "01a13a5b-7e10-7c44-8f0b-2d6a9e4c1f35", "stateChangedAt": _AFTERWARDS}
_HANDED_BACK = {
    **_SET_BY_A_PERSON, "id": "01a13a5b-7e10-7c44-8f0b-2d6a9e4c1f36", "stateChangedAt": _AFTERWARDS,
    "reason": None, "origin": "reset",
}
# Same instant as the person's row: only the id tells which one OpenCRQ reads as the latest.
_HANDED_BACK_IN_THE_SAME_MS_BEFORE = {**_HANDED_BACK, "id": "01a13a52-0c4e-7b31-9d2a-5e8f1c3b7a8f",
                                      "stateChangedAt": _SET_BY_A_PERSON["stateChangedAt"]}
_HANDED_BACK_IN_THE_SAME_MS_AFTER = {**_HANDED_BACK_IN_THE_SAME_MS_BEFORE, "id": "01a13a52-0c4e-7b31-9d2a-5e8f1c3b7a91"}


def _in_both_orders(history: list[dict], id: str, *values: Any) -> list:
    """*history* oldest first, as OpenCRQ serves it, then newest first, each followed by *values*."""
    return [
        pytest.param(history, *values, id=f"{id}, oldest first"),
        pytest.param(history[::-1], *values, id=f"{id}, newest first"),
    ]


@pytest.mark.parametrize(
    "history",
    [
        *_in_both_orders(_SET_ASIDE["history"], "set aside by a person"),
        *_in_both_orders([_HANDED_BACK_IN_THE_SAME_MS_BEFORE, _SET_BY_A_PERSON], "same instant, person's id larger"),
    ],
)
async def test_an_evidence_set_by_a_person_on_a_live_connector_is_handed_back(history: list[dict]):
    detail = {**_SET_ASIDE, "history": history}
    with _recorded_trpc(opencrq_assets, {"supportingAssets.getEvidenceDetail": detail}) as calls:
        out = await _run("reset_opencrq_evidence_state", evidence_id=EVIDENCE)
    assert calls == [
        _sent("supportingAssets.getEvidenceDetail", {"evidenceId": EVIDENCE}),
        # supporting-assets/supporting-assets-router.ts: resetEvidenceState input is {evidenceId}
        _sent("supportingAssets.resetEvidenceState", {"evidenceId": EVIDENCE}, mutation=True),
    ]
    assert out == (
        f"Handed evidence {EVIDENCE} back to its connector; the state "
        "set by a person stays until the connector's next pass."
    )


@pytest.mark.parametrize(
    ("history", "origin"),
    [
        *_in_both_orders([*_SET_ASIDE["history"], _REPORTED_AFTERWARDS], "reported again afterwards", "exposed"),
        *_in_both_orders([*_SET_ASIDE["history"], _HANDED_BACK], "already handed back", "reset"),
        *_in_both_orders(
            [_SET_BY_A_PERSON, _HANDED_BACK_IN_THE_SAME_MS_AFTER], "same instant, reset's id larger", "reset",
        ),
    ],
)
async def test_an_evidence_whose_latest_state_no_person_set_is_refused_whatever_the_order(history: list[dict], origin: str):
    detail = {**_SET_ASIDE, "history": history}
    with _recorded_trpc(opencrq_assets, {"supportingAssets.getEvidenceDetail": detail}) as calls:
        out = await _run("reset_opencrq_evidence_state", evidence_id=EVIDENCE)
    assert [c["procedure"] for c in calls] == ["supportingAssets.getEvidenceDetail"]
    assert out == (
        f"The latest state of evidence {EVIDENCE} was not set by a person "
        f"(origin: {origin}), so there is nothing to hand back. Nothing was changed."
    )


@pytest.mark.parametrize(
    ("detail", "refusal"),
    [
        pytest.param(
            _evidence_detail(sources=[], history=[_SET_BY_A_PERSON]),
            "has no live connector source (attached by hand, or its connector was deleted)", id="attached by hand",
        ),
        pytest.param(
            _evidence_detail(sources=[{"connectorId": None, "source": None}], history=[_SET_BY_A_PERSON]),
            "has no live connector source", id="connector deleted",
        ),
        pytest.param(_evidence_detail(history=[]), "has no history", id="no history"),
        pytest.param(_evidence_detail(), "was not set by a person (origin: connector)", id="recorded, set by the connector"),
    ],
)
async def test_an_evidence_reset_that_opencrq_would_refuse_is_refused_before_the_mutation(detail: dict, refusal: str):
    with _recorded_trpc(opencrq_assets, {"supportingAssets.getEvidenceDetail": detail}) as calls:
        out = await _run("reset_opencrq_evidence_state", evidence_id=EVIDENCE)
    assert [c["procedure"] for c in calls] == ["supportingAssets.getEvidenceDetail"]
    assert refusal in out
    assert "Nothing was changed." in out


async def test_resetting_an_evidence_that_does_not_exist_says_so():
    with _recorded_trpc(opencrq_assets, {"supportingAssets.getEvidenceDetail": None}) as calls:
        out = await _run("reset_opencrq_evidence_state", evidence_id=EVIDENCE)
    assert len(calls) == 1
    assert out == f"Evidence {EVIDENCE} not found."


@pytest.mark.parametrize("tool", ["delete_opencrq_asset_evidence", "reset_opencrq_evidence_state"])
def test_an_evidence_action_says_where_the_evidence_id_comes_from(tool: str):
    description = _tool_description(tool)
    for source in ("create_opencrq_asset_evidence's answer", "list_opencrq_cve_evidences", "get_opencrq_evidence"):
        assert source in description


def test_changing_an_asset_s_category_is_said_to_drop_its_hand_typed_properties():
    description = _tool_description("update_opencrq_supporting_asset")
    assert "Changing the category drops a subcategory that no longer fits" in description
    assert "stops applying until the asset is moved back to it" in description


async def test_an_evidence_attached_by_hand_is_shown_without_sources():
    detail = {**_fixture("supportingAssets.getEvidenceDetail")[1], "sources": [], "history": [
        {"state": "applicable", "stateChangedAt": "2026-10-14T10:47:58.622Z", "origin": "user", "changedByUserName": "Jane"},
    ]}
    with _recorded_trpc(opencrq_assets, {"supportingAssets.getEvidenceDetail": detail}):
        out = await _run("get_opencrq_evidence", evidence_id=EVIDENCE)
    assert "Sources: none (attached by hand)" in out
    assert "applicable by Jane [origin: user]" in out


async def test_a_source_whose_connector_was_deleted_is_shown_as_such():
    detail = {**_fixture("supportingAssets.getEvidenceDetail")[1], "sources": [
        {"connectorId": None, "source": None, "lastSeenAt": None},
    ]}
    with _recorded_trpc(opencrq_assets, {"supportingAssets.getEvidenceDetail": detail}):
        out = await _run("get_opencrq_evidence", evidence_id=EVIDENCE)
    assert "unknown source, connector deleted, last seen never" in out


_HAND_SET_ASSET = _fixture("supportingAssets.getSupportingAsset.hand-set")[1]


async def test_resetting_an_asset_reads_it_then_hands_its_hand_set_values_back_to_the_connectors():
    with _recorded_trpc(opencrq_assets, {"supportingAssets.getSupportingAsset": _HAND_SET_ASSET}) as calls:
        out = await _run("reset_opencrq_supporting_asset_to_connectors", supporting_asset_id=SUPPORTING_ASSET)
    assert calls == [
        _sent("supportingAssets.getSupportingAsset", {"id": SUPPORTING_ASSET}),
        # supporting-assets-router.ts: resyncSupportingAssetWithConnectors input is {id}
        _sent("supportingAssets.resyncSupportingAssetWithConnectors", {"id": SUPPORTING_ASSET}, mutation=True),
    ]
    assert out == (
        f"Reset supporting asset {SUPPORTING_ASSET}: name, linked person, property block "
        "went back to what microsoft_graph report. Tags and owner were kept."
    )


async def test_resetting_an_asset_opencrq_lists_nothing_hand_set_on_says_what_it_listed_not_what_the_reset_did():
    detail = {**_HAND_SET_ASSET, "manualValues": [], "manualProperties": None}
    with _recorded_trpc(opencrq_assets, {"supportingAssets.getSupportingAsset": detail}) as calls:
        out = await _run("reset_opencrq_supporting_asset_to_connectors", supporting_asset_id=SUPPORTING_ASSET)
    assert [c["procedure"] for c in calls] == [
        "supportingAssets.getSupportingAsset", "supportingAssets.resyncSupportingAssetWithConnectors",
    ]
    assert out == (
        f"Resynced supporting asset {SUPPORTING_ASSET} with microsoft_graph: OpenCRQ listed "
        "no hand-set value before the reset. Tags and owner were kept."
    )


@pytest.mark.parametrize(
    "detail",
    [
        pytest.param(_fixture("supportingAssets.getSupportingAsset")[1], id="recorded, neither key"),
        pytest.param(
            {k: v for k, v in _HAND_SET_ASSET.items() if k != "manualProperties"}, id="manual values only",
        ),
        pytest.param({k: v for k, v in _HAND_SET_ASSET.items() if k != "manualValues"}, id="manual properties only"),
    ],
)
async def test_resetting_an_asset_whose_read_does_not_list_its_hand_set_values_never_says_none_were(detail: dict):
    with _recorded_trpc(opencrq_assets, {"supportingAssets.getSupportingAsset": detail}) as calls:
        out = await _run("reset_opencrq_supporting_asset_to_connectors", supporting_asset_id=SUPPORTING_ASSET)
    assert [c["procedure"] for c in calls] == [
        "supportingAssets.getSupportingAsset", "supportingAssets.resyncSupportingAssetWithConnectors",
    ]
    assert out == (
        f"Reset supporting asset {SUPPORTING_ASSET} with microsoft_graph. OpenCRQ's read of the "
        "asset did not list its hand-set values, so which of them went back to what the "
        "connectors report is not known. Tags and owner were kept."
    )


@pytest.mark.parametrize(
    "sources",
    [
        pytest.param([], id="attached by hand"),
        pytest.param([{"connectorId": None, "source": "microsoft_graph", "origin": "connector"}], id="connector deleted"),
        pytest.param([{"connectorId": None, "source": None, "origin": "user"}], id="manual observation only"),
    ],
)
async def test_resetting_an_asset_no_live_connector_observes_changes_nothing(sources: list):
    detail = {**_fixture("supportingAssets.getSupportingAsset")[1], "sources": sources}
    with _recorded_trpc(opencrq_assets, {"supportingAssets.getSupportingAsset": detail}) as calls:
        out = await _run("reset_opencrq_supporting_asset_to_connectors", supporting_asset_id=SUPPORTING_ASSET)
    assert calls == [_sent("supportingAssets.getSupportingAsset", {"id": SUPPORTING_ASSET})]
    assert "Nothing to resync" in out
    assert "left untouched" in out


def test_resetting_an_asset_states_what_is_lost_and_what_is_kept():
    description = _tool_description("reset_opencrq_supporting_asset_to_connectors")
    for expected in ("set by hand", "cannot be undone", "Tags and the owner are kept", "live connector", "Confirm with the user"):
        assert expected in description
    assert "service account" not in description


def test_organization_creation_says_a_service_account_is_refused():
    assert "Needs a user session (a service account is refused)." in _tool_description("create_opencrq_organization")


@pytest.mark.parametrize(
    ("args", "refusal"),
    [
        pytest.param({"reaction_time_min_hours": 1}, "must be provided together", id="an incomplete triplet"),
        pytest.param(
            {"reaction_time_min_hours": 5, "reaction_time_likely_hours": 4, "reaction_time_max_hours": 12},
            "min <= likely <= max", id="a triplet out of order",
        ),
        pytest.param(
            {"reaction_time_min_hours": 0, "reaction_time_likely_hours": 0, "reaction_time_max_hours": 12},
            "greater than 0", id="a likely time of zero",
        ),
        pytest.param({"estate_mode": "half"}, "estate_mode must be one of", id="an unknown estate mode"),
        pytest.param(
            {"mapped_asset_ids_added": [f"00000000-0000-4000-8000-{n:012d}" for n in range(5001)]},
            "at most 5000", id="more assets than one save maps",
        ),
        pytest.param(
            {"clear_reaction_time": True, "reaction_time_min_hours": 0, "reaction_time_likely_hours": 1,
             "reaction_time_max_hours": 2},
            "not both", id="a clear next to a value",
        ),
    ],
)
async def test_organization_update_refuses_what_opencrq_would_refuse_before_calling_it(args: dict, refusal: str):
    with _recorded_trpc(opencrq_admin, {}) as calls:
        out = await _run("update_opencrq_organization", organization_id=ORGANIZATION, **args)
    assert refusal in out
    assert calls == []


async def test_supporting_asset_creation_sends_every_field_opencrq_accepts():
    with _recorded_trpc(opencrq_assets, {}) as calls:
        out = await _run(
            "create_opencrq_supporting_asset",
            name="ACME-SRV-0500", category="host", exposure="internal", criticality="high",
            subcategory="server", url="https://cmdb.acme.io/srv-0500", linked_person="Jane Doe",
            tag_names=["primary-asset", " edr "],
            properties={
                "family": "machine", "hostname": "acme-srv-0500", "ip_addresses": ["10.0.0.5"],
                "cloud_provider": "aws", "arch": "x86_64", "is_eol": False,
            },
            organization_id=ORGANIZATION,
        )
    (call,) = calls
    asset_id = call["input"]["id"]
    assert uuid.UUID(asset_id)
    assert call == _sent(
        "supportingAssets.createSupportingAsset",
        # supporting-assets/supporting-assets-router.ts: createSupportingAsset, MANUAL_MACHINE_PROPERTIES_SCHEMA,
        # plus organizationId from app-router/estate.ts
        {
            "id": asset_id, "name": "ACME-SRV-0500", "description": None, "ownerId": None,
            "exposure": "internal", "categoryId": "host", "criticality": "high",
            "subcategoryId": "server", "url": "https://cmdb.acme.io/srv-0500", "linkedPerson": "Jane Doe",
            "tagNames": ["primary-asset", "edr"],
            "properties": {
                "family": "machine", "hostname": "acme-srv-0500", "ipAddresses": ["10.0.0.5"],
                "cloudProvider": "aws", "arch": "x86_64", "isEol": False,
            },
            "organizationId": ORGANIZATION,
        },
        mutation=True,
    )
    assert f"(id: {asset_id})" in out


async def test_a_security_platform_asset_is_sent_with_its_groups_and_owner_name_and_the_owner_outcome_is_said() -> None:
    answers = {"supportingAssets.createSupportingAsset": {"owner": {"name": "jane@acme.io", "outcome": "resolved"}}}
    with _recorded_trpc(opencrq_assets, answers) as calls:
        out = await _run(
            "create_opencrq_supporting_asset",
            name="ACME EDR",
            category="security_platform",
            subcategory="edr",
            owner_name=" jane@acme.io ",
            groups=[{"name": "PCI scope", "external_reference": "grp-1"}, {"name": " Tier 1 "}],
        )
    (call,) = calls
    asset_id = call["input"]["id"]
    assert call == _sent(
        "supportingAssets.createSupportingAsset",
        # supporting-assets-router.ts: createSupportingAsset, ownerName (opencrq #3159), groups (opencrq #3167)
        {
            "id": asset_id,
            "name": "ACME EDR",
            "description": None,
            "ownerId": None,
            "exposure": "unknown",
            "categoryId": "security_platform",
            "subcategoryId": "edr",
            "ownerName": "jane@acme.io",
            "groups": [
                {"name": "PCI scope", "externalReference": "grp-1"},
                {"name": "Tier 1", "externalReference": None},
            ],
        },
        mutation=True,
    )
    assert "Owner 'jane@acme.io': set to the tenant member it names." in out


@pytest.mark.parametrize(
    ("outcome", "said"),
    [
        ("unresolved", "no tenant member matches it, the owner is unchanged"),
        ("ambiguous", "several tenant members match it, the owner is unchanged"),
        ("kept_manual", "the owner a person set is kept"),
    ],
)
async def test_an_update_with_an_owner_name_says_what_opencrq_did_with_it(outcome: str, said: str) -> None:
    answers = {"supportingAssets.updateSupportingAsset": {"owner": {"name": "Jane Doe", "outcome": outcome}}}
    with _recorded_trpc(opencrq_assets, answers) as calls:
        out = await _run(
            "update_opencrq_supporting_asset",
            supporting_asset_id=SUPPORTING_ASSET,
            owner_name="Jane Doe",
            groups=[],
        )
    assert calls == [
        _sent(
            "supportingAssets.updateSupportingAsset",
            {"id": SUPPORTING_ASSET, "ownerName": "Jane Doe", "groups": []},
            mutation=True,
        )
    ]
    assert f"Owner 'Jane Doe': {said}." in out


@pytest.mark.parametrize(
    "groups",
    [[{"external_reference": "grp-1"}], [{"name": "x" * 257}], ["PCI scope"]],
    ids=["a group without a name", "a name past 256 characters", "a group that is not an object"],
)
async def test_a_group_opencrq_would_refuse_is_refused_before_any_call(groups: list[object]) -> None:
    with _recorded_trpc(opencrq_assets, {}) as calls:
        result = await _execute(
            "create_opencrq_supporting_asset", name="ACME EDR", category="security_platform", groups=groups
        )
    assert "group" in result.content
    assert calls == []


@pytest.mark.parametrize(
    ("args", "refusal"),
    [
        pytest.param(
            {"subcategory": "firewall"}, "does not belong to category 'host'",
            id="a subcategory of another category than the asset's",
        ),
        pytest.param(
            {"properties": {"family": "identity", "department": "IT"}}, "takes a 'machine' block",
            id="a block of another family than the asset's",
        ),
    ],
)
async def test_supporting_asset_update_reads_the_category_it_was_not_given_and_refuses_a_mismatch(
    args: dict, refusal: str
):
    answers = _answers(("supportingAssets.getSupportingAsset",), {})
    with _recorded_trpc(opencrq_assets, answers) as calls:
        out = await _run("update_opencrq_supporting_asset", supporting_asset_id=SUPPORTING_ASSET, **args)
    assert refusal in out
    assert calls == [_sent("supportingAssets.getSupportingAsset", {"id": SUPPORTING_ASSET})]


async def test_supporting_asset_update_checks_a_subcategory_and_a_block_against_the_current_category():
    answers = _answers(("supportingAssets.getSupportingAsset",), {})
    with _recorded_trpc(opencrq_assets, answers) as calls:
        await _run(
            "update_opencrq_supporting_asset", supporting_asset_id=SUPPORTING_ASSET,
            subcategory="laptop", properties={"vendor": "Dell", "model": "Latitude"},
        )
    assert calls == [
        _sent("supportingAssets.getSupportingAsset", {"id": SUPPORTING_ASSET}),
        _sent(
            "supportingAssets.updateSupportingAsset",
            {
                "id": SUPPORTING_ASSET, "subcategoryId": "laptop",
                "properties": {"family": "machine", "vendor": "Dell", "model": "Latitude"},
            },
            mutation=True,
        ),
    ]


async def test_support_to_support_link_creation_sends_provider_and_dependent():
    provider, dependent = CORE_BANKING, "15c5d6e7-f8a9-4b0c-ad2e-3f4a5b6c7d8e"
    with _recorded_trpc(
        opencrq_assets, {"assetDependencies.createSupportToSupportLinks": {"goneIds": []}}
    ) as calls:
        out = await _run(
            "create_opencrq_asset_dependency",
            link_type="support_to_support", provider_asset_id=provider, dependent_asset_id=dependent,
        )
    (call,) = calls
    (link,) = call["input"]
    assert uuid.UUID(link["id"])
    assert call == _sent(
        "assetDependencies.createSupportToSupportLinks",
        # asset-dependencies/asset-dependencies-router.ts: createSupportToSupportLinks, an array without roles
        [{"id": link["id"], "providerAssetId": provider, "dependentAssetId": dependent}],
        mutation=True,
    )
    assert f"Created support_to_support dependency link (id: {link['id']})" in out


# -- Behaviours the recorded responses do not show ------------------------------


def test_loss_driver_tools_offer_no_attack_outcome_filter():
    for tool in ("list_opencrq_loss_drivers", "get_opencrq_loss_driver"):
        assert "attack_outcome_id" not in _tool_properties(tool), tool


async def test_a_whole_list_is_read_page_by_page_from_an_offset():
    answers = _answers(("drata.getControlsForFramework",), {})
    with _recorded_trpc(opencrq_frameworks, answers):
        first = await _run("list_opencrq_drata_controls", framework_id=ISO_FRAMEWORK)
        rest = await _run("list_opencrq_drata_controls", framework_id=ISO_FRAMEWORK, offset=1)
        past = await _run("list_opencrq_drata_controls", framework_id=ISO_FRAMEWORK, offset=9)
    assert "DCF-1001" in first and "Showing" not in first
    assert "DCF-1001" not in rest and "DCF-1002" in rest and rest.endswith("Showing 2-3 of 3.")
    assert past.endswith("offset 9 is past the end: 3 in total.")


async def test_a_scenario_without_loss_estimates_is_not_shown_zero_totals():
    _, zeros = _fixture("scenarioLossEstimates.getFinancialSummary")
    zeros = {**zeros, "totalMinDollar": 0, "totalLikelyDollar": 0, "totalMaxDollar": 0,
             "pertExpectedValue": 0, "updatedAt": None}
    answers = {
        "scenarioLossEstimates.getFinancialSummary": zeros,
        "scenarioLossEstimates.getScenarioLossEstimates": [],
    }
    with _recorded_trpc(opencrq_risk, answers):
        out = await _run("get_opencrq_financial_summary", risk_scenario_id=SCENARIO)
    assert out == "No loss estimates recorded for this risk scenario."


async def test_the_financial_summary_names_its_warnings_and_a_pending_recompute():
    answers = _answers(
        ("scenarioLossEstimates.getFinancialSummary", "scenarioLossEstimates.getScenarioLossEstimates"),
        {},
    )
    answers["scenarioLossEstimates.getFinancialSummary"] = {
        **answers["scenarioLossEstimates.getFinancialSummary"],
        "recomputePending": True,
        "sanityWarnings": [
            {"kind": "over_pricing", "expectedTotalUsd": 42842531.3, "anchorUsd": 5000000, "ratio": 8.57}
        ],
    }
    with _recorded_trpc(opencrq_risk, answers):
        out = await _run("get_opencrq_financial_summary", risk_scenario_id=SCENARIO)
    assert "Recompute pending" in out
    assert (
        "Sanity warning (over-pricing): expected total 42,842,531 against an anchor of "
        "5,000,000 (ratio 8.57)"
    ) in out


async def test_a_framework_is_labelled_by_the_source_it_was_read_from():
    _, catalog = _fixture("complianceAssessments.getFrameworks")
    with_slug = [{**f, "slug": "nist-csf"} for f in catalog]
    answers = {"complianceAssessments.getFrameworks": with_slug, "drata.getFrameworks": []}
    with _recorded_trpc(opencrq_frameworks, answers):
        out = await _run("list_opencrq_frameworks")
    assert "NIST CSF 2.0 (assessment catalog)" in out
    assert "Drata mirror)" not in out


async def test_an_attack_id_is_looked_up_whatever_its_case():
    answers = _answers(("mitreAttack.getTechniqueByExternalId",), {})
    with _recorded_trpc(opencrq_mitre, answers) as calls:
        await _run("get_opencrq_mitre_technique", technique_id="t1566")
    assert calls[0] == _sent("mitreAttack.getTechniqueByExternalId", {"externalId": "T1566"})


async def test_a_page_past_the_end_is_not_reported_as_no_data():
    with _recorded_trpc(opencrq_assets, {"cve.list": {"items": [], "total": 312}}):
        out = await _run("list_opencrq_cves", page=99)
    assert out == "Page 99 is past the end: 312 in total."


async def test_mitre_technique_search_does_not_match_a_description():
    _, rows = _fixture("mitreAttack.getTechniques")
    described = [dict(row, description="Adversaries stage collected archives") for row in rows]
    with _recorded_trpc(opencrq_mitre, {"mitreAttack.getTechniques": described}):
        out = await _run("search_opencrq_mitre_techniques", search="collected archives")
    assert out == "No MITRE techniques matched."


# -- A service account refused by OpenCRQ ---------------------------------------


def _refusal(url: str) -> httpx.Response:
    """The tRPC envelope of the 500 OpenCRQ answers a service account on a tenant-scoped read."""
    return httpx.Response(
        500,
        json={
            "error": {
                "message": "Tenant context required",
                "code": -32603,
                "data": {"code": "INTERNAL_SERVER_ERROR", "httpStatus": 500},
            }
        },
        request=httpx.Request("GET", url),
    )


async def test_service_account_refusal_is_an_error_output_not_an_exception():
    async def refuse(self: httpx.AsyncClient, method: str, url: str, **kwargs: Any) -> httpx.Response:
        return _refusal(url)

    with patch.object(httpx.AsyncClient, "request", refuse):
        result = await _execute("list_opencrq_unique_findings")
    assert result.is_error
    assert result.content.startswith("Error: OpenCRQ server error (500)")
    assert "list unique findings" in result.content


_USER_SESSION_HINT = "OpenCRQ answers this action with a 500 there: it needs a user session."


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        pytest.param(
            "manage_opencrq_scope_control_posture",
            {"action": "clear", "organization_id": ORGANIZATION, "capability_id": CAPABILITY, "dimension": "strength"},
            id="scope control posture",
        ),
        pytest.param(
            "manage_opencrq_capability_techniques",
            {"action": "reset", "capability_id": CAPABILITY, "function": "detect"},
            id="capability techniques",
        ),
        pytest.param(
            "update_opencrq_control_capability_assessment",
            {"capability_id": CAPABILITY, "strength": "L1"},
            id="capability assessment",
        ),
        pytest.param("create_opencrq_organization", {"name": "Retail"}, id="organization creation"),
    ],
)
async def test_a_client_only_mutation_refused_with_a_500_says_it_needs_a_user_session(tool: str, args: dict):
    async def refuse(self: httpx.AsyncClient, method: str, url: str, **kwargs: Any) -> httpx.Response:
        return _refusal(url)

    with patch.object(httpx.AsyncClient, "request", refuse):
        result = await _execute(tool, **args)
    assert result.is_error
    assert "OpenCRQ server error (500)" in result.content
    assert result.content.endswith(_USER_SESSION_HINT)
    assert "If this connection uses a service account" in result.content


async def test_a_client_only_mutation_failing_otherwise_is_not_blamed_on_the_token():
    async def reject(self: httpx.AsyncClient, method: str, url: str, **kwargs: Any) -> httpx.Response:
        return httpx.Response(
            400,
            json={"error": {"message": "Invalid input", "code": -32600}},
            request=httpx.Request("POST", url),
        )

    with patch.object(httpx.AsyncClient, "request", reject):
        result = await _execute("create_opencrq_organization", name="Retail")
    assert result.is_error
    assert _USER_SESSION_HINT not in result.content


async def test_a_500_that_is_not_a_trpc_error_gets_no_hint():
    plain = "Error: OpenCRQ server error (500) while trying to create an organization."
    assert opencrq_common._client_only_hint(plain) == plain
    network = opencrq_common.TrpcError("Error: OpenCRQ server error (500) text", None, None)
    assert opencrq_common._client_only_hint(network) == network
    refused = opencrq_common.TrpcError("Error: OpenCRQ failed: nope", None, 500)
    assert opencrq_common._client_only_hint(refused).endswith(_USER_SESSION_HINT)
    rejected = opencrq_common.TrpcError("Error: OpenCRQ server error (500)", None, 400)
    assert opencrq_common._client_only_hint(rejected) == rejected


async def test_a_refused_section_is_named_and_does_not_hide_the_rest_of_the_answer():
    _, scenario = _fixture("riskScenarios.getRiskScenario")

    async def answer(self: httpx.AsyncClient, method: str, url: str, **kwargs: Any) -> httpx.Response:
        if url.endswith("/riskScenarios.getRiskScenario"):
            return httpx.Response(
                200, json={"result": {"data": scenario}}, request=httpx.Request("GET", url)
            )
        return _refusal(url)

    with patch.object(httpx.AsyncClient, "request", answer):
        out = await _run("get_opencrq_risk_scenario", risk_scenario_id=SCENARIO)
    assert "[RiskScenario] Data Breach on Retail Lending Platform" in out
    assert (
        "Mitigating control capabilities: lookup failed - Error: OpenCRQ server error (500)"
    ) in out


# -- A control is assessed on strength and coverage only ---------------------------


def _all_strings(node: Any):
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for key, value in node.items():
            yield str(key)
            yield from _all_strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from _all_strings(value)


@pytest.mark.parametrize("tool", ["update_opencrq_control_capability_assessment", "manage_opencrq_scope_control_posture"])
def test_a_control_assessment_tool_offers_no_reliability(tool: str):
    executor = CallableToolExecutor()
    definition = next(t for t in build_tools(TOKEN, executor) if t.name == tool)
    assert "reliability" not in definition.input_schema["properties"]
    assert not [s for s in _all_strings(definition.input_schema) if "eliab" in s]
    assert "eliab" not in definition.description


def test_the_axes_a_tool_re_attaches_or_clears_are_strength_and_coverage():
    update = _tool_properties("update_opencrq_control_capability_assessment")
    assert update["reattach"]["items"]["enum"] == ["strength", "coverage"]
    assert _tool_properties("manage_opencrq_scope_control_posture")["dimension"]["enum"] == ["strength", "coverage"]


async def test_a_reattachment_of_reliability_is_refused_before_any_call():
    with _recorded_trpc(opencrq_frameworks, {}) as calls:
        result = await _execute(
            "update_opencrq_control_capability_assessment", capability_id=CAPABILITY, reattach=["reliability"],
        )
    assert calls == []
    assert "reattach must name axes among: strength, coverage." in result.content


async def test_a_scope_clear_of_reliability_is_refused_before_any_call():
    with _recorded_trpc(opencrq_frameworks, {}) as calls:
        result = await _execute(
            "manage_opencrq_scope_control_posture",
            action="clear", organization_id=ORGANIZATION, capability_id=CAPABILITY, dimension="reliability",
        )
    assert calls == []
    assert "dimension must be one of: strength, coverage." in result.content


async def test_a_declared_posture_never_sends_or_keeps_a_reliability_level():
    row = {**_scope_posture(strength=("L3", "scope")), "reliabilityLevel": "L1", "reliabilitySource": "scope"}
    out, calls = await _declare_posture([row], coverage="L0")
    assert "reliabilityLevel" not in calls[1]["input"]
    assert "eliab" not in out


def test_the_what_if_description_does_not_name_reliability():
    assert "eliab" not in _tool_description("get_opencrq_crq_what_if")
