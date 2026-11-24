"""Contract tests of the OpenCRQ read tools on findings, threats, risk treatment,
proven control posture, the risk dashboard and connectors.

Same shape as ``test_opencrq_tool_contracts.py``: each case drives one tool with
``_trpc`` patched and pins the procedures it calls and the exact input it sends,
the file of each named in a comment relative to ``back/src/features/app-router``
of the OpenCRQ repository. The patched ``_trpc`` answers with a response recorded
from a running OpenCRQ and trimmed (``tests/data/opencrq``); the tool's output
must carry the recorded values and no placeholder where the response has one.
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from app.api.v1.mcp_platform import OPENCRQ_MCP_READ_TOOLS, OPENCRQ_MCP_TOOLS
from app.integrations.providers.opencrq import _common as opencrq_common
from app.integrations.providers.opencrq import (
    build_tools,
    connectors,
    control_validation,
    dashboard,
    findings,
    threats,
    treatment,
)
from app.integrations.providers.opencrq._common import (
    _grc_link,
    _grc_primary_asset_link,
    _grc_scenario_link,
)
from app.tools.base import CallableToolExecutor, ToolCall

BASE = "https://demo.opencrq.io"
TOKEN = f"{BASE}||test-token"
DATA = Path(__file__).parent / "data" / "opencrq"

TENANT = "89c70e9a-8317-4da7-8765-1b9ba004c32c"
ORGANIZATION = "90b57256-32e4-4369-b768-fa6f2bb64fee"
LOG4SHELL = "CVE-2021-44228"
TRADING_PLATFORM = "08b8c9d0-e1f2-4a3b-9c5d-6e7f8a9b0c1d"
SANDWORM = "01a139e7-7e09-7c47-91b9-b77217c31c8c"
SCENARIO = "019edb28-0011-7000-8000-000000000002"
PLAN = "019edb28-0022-7000-8000-000000000002"
REVIEW = "019edb28-003a-7000-8000-000000000001"
EDR = "4c5d8d76-763f-5b20-8b5c-fe80d72682eb"

_MODULES = (opencrq_common, findings, threats, treatment, control_validation, dashboard, connectors)

MCP_TOOLS = {
    "get_opencrq_findings_summary",
    "get_opencrq_top_vulnerabilities",
    "list_opencrq_findings",
    "get_opencrq_cve_finding",
    "get_opencrq_asset_posture",
    "get_opencrq_technique_test_posture",
    "list_opencrq_threats",
    "get_opencrq_threat",
    "list_opencrq_scenario_threats",
    "list_opencrq_cve_threats",
    "list_opencrq_treatment_plans",
    "get_opencrq_treatment_plan",
    "list_opencrq_budget_reviews",
    "get_opencrq_budget_review",
    "get_opencrq_control_validation",
    "get_opencrq_risk_dashboard",
}

CONNECTOR_TOOLS = {"list_opencrq_connectors", "list_opencrq_connector_syncs"}


#: The instant the threat tools read as "now": the recorded dates are fixed, so is the clock.
NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


class _ClockAt(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)


@pytest.fixture(autouse=True)
def _threats_clock(monkeypatch):
    monkeypatch.setattr(threats, "datetime", _ClockAt)


def _fixture(name: str) -> tuple[str, Any]:
    doc = json.loads((DATA / f"{name}.json").read_text(encoding="utf-8"))
    return doc["procedure"], doc["data"]


@contextlib.contextmanager
def _recorded_trpc(answers: dict[str, Any]):
    """Patch ``_trpc`` in every module the tools read through; yield the recorded calls.

    An answer that is a string starting with ``Error:`` is returned as the call's error.
    """
    calls: list[dict] = []

    async def fake(
        base_url, headers, procedure, action, *, input=None, mutation=False,
        tenant_id="", client=None,
    ):
        calls.append(
            {"procedure": procedure, "input": input, "mutation": mutation, "tenant_id": tenant_id}
        )
        answer = answers.get(procedure)
        if isinstance(answer, str) and answer.startswith("Error:"):
            return None, answer
        return answer, None

    with contextlib.ExitStack() as stack:
        for module in _MODULES:
            stack.enter_context(patch.object(module, "_trpc", fake))
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


def _sent(procedure: str, input: Any = None) -> dict:
    return {"procedure": procedure, "input": input, "mutation": False, "tenant_id": TENANT}


def _assert_no_placeholder_for_a_value(out: str, answers: dict[str, Any]) -> None:
    """A placeholder in the output is only legitimate when the response itself carries that text."""
    recorded = json.dumps(answers, ensure_ascii=False)
    for placeholder in ("N/A", "None", "?"):
        if placeholder not in recorded:
            assert placeholder not in out, f"{placeholder!r} shown in:\n{out}"


# -- Registration and exposure --------------------------------------------------


def test_new_tools_are_registered_read_only():
    executor = CallableToolExecutor()
    tools = {t.name: t for t in build_tools(TOKEN, executor)}
    assert tools.keys() >= MCP_TOOLS | CONNECTOR_TOOLS
    for name in MCP_TOOLS | CONNECTOR_TOOLS:
        assert name.startswith(("get_", "list_")), name
        assert name in executor._handlers, name


def test_business_reads_are_exposed_over_mcp_and_connectors_are_not():
    assert MCP_TOOLS <= OPENCRQ_MCP_TOOLS
    assert MCP_TOOLS <= OPENCRQ_MCP_READ_TOOLS
    assert not CONNECTOR_TOOLS & OPENCRQ_MCP_TOOLS


# -- Reads ----------------------------------------------------------------------


@dataclass(frozen=True)
class Read:
    tool: str
    args: dict
    calls: list[dict]
    fixtures: tuple[str, ...]
    expect: tuple[str, ...]
    overrides: dict = field(default_factory=dict)


READS = {
    "get_opencrq_findings_summary": Read(
        "get_opencrq_findings_summary", {},
        [
            # finding-score/finding-score-router.ts: getFindingsPanelCounts and
            # getOpenFindingFigures take estateInput, organizationId defaulting to null
            _sent("findingScore.getFindingsPanelCounts", {}),
            _sent("findingScore.getOpenFindingFigures", {}),
        ],
        ("findingScore.getFindingsPanelCounts", "findingScore.getOpenFindingFigures"),
        (
            "Findings summary (whole tenant):", "Open findings: 28 on 16 of 40 assets",
            "By band: low 17 (+2 new), medium 4 (+0 new) (new since 2026-10-07)",
            "Critical: 0, internet-exposed: 8, known exploited (CISA KEV): 21, "
            "open for more than 90 days: 19",
            "7 findings wait for a CVSS vector",
        ),
    ),
    "get_opencrq_findings_summary_organization": Read(
        "get_opencrq_findings_summary", {"organization_id": ORGANIZATION},
        [
            # organizations/organizations-router.ts: getOrganization input is {id}
            _sent("organizations.getOrganization", {"id": ORGANIZATION}),
            _sent("findingScore.getFindingsPanelCounts", {"organizationId": ORGANIZATION}),
            _sent("findingScore.getOpenFindingFigures", {"organizationId": ORGANIZATION}),
        ],
        (
            "organizations.getOrganization", "findingScore.getFindingsPanelCounts",
            "findingScore.getOpenFindingFigures",
        ),
        ("Findings summary (Acme Bank, measured over the whole tenant estate):",),
    ),
    "get_opencrq_top_vulnerabilities": Read(
        "get_opencrq_top_vulnerabilities", {},
        # finding-score/finding-score-router.ts: getTopVulnerabilities takes estateInput
        [_sent("findingScore.getTopVulnerabilities", {})],
        ("findingScore.getTopVulnerabilities",),
        (
            "Top 3 vulnerabilities by finding score (whole tenant):",
            "1. CVE-2021-44228 (Apache Log4j2 JNDI Remote Code Execution (Log4Shell)): score 52, "
            "3 impacted assets, reported by microsoft_graph",
            f"{BASE}/{TENANT}/findings/cve/CVE-2021-44228/overview",
            "3. CVE-2023-4966",
        ),
    ),
    "list_opencrq_findings": Read(
        "list_opencrq_findings", {"limit": 3},
        # finding-score/finding-score-router.ts: listScoredFindings, pagedInput + estateInput +
        # state defaulting to applicable
        [_sent("findingScore.listScoredFindings", {
            "page": 1, "limit": 3, "sortBy": "score", "sortDesc": True, "state": "applicable",
        })],
        ("findingScore.listScoredFindings",),
        (
            "Findings (whole tenant; page 1, showing 3 of 37):",
            "CVE-2021-44228 (Apache Log4j2 JNDI Remote Code Execution (Log4Shell)) on Trading "
            "Platform (Murex): score 52 (medium), applicable [unknown, criticality unknown]",
            "on Internet Banking Portal: score 43 (medium), applicable [internet_facing",
            "(finding id: 019edb28-000b-7000-8000-000000000023)",
        ),
    ),
    "list_opencrq_findings_of_a_cve": Read(
        "list_opencrq_findings", {"cve_id": "cve-2021-44228", "state": "all", "limit": 3},
        # the vulnerability filter is a discriminated union, {kind: 'cve', cveId}; a null state
        # reads both states
        [_sent("findingScore.listScoredFindings", {
            "page": 1, "limit": 3, "sortBy": "score", "sortDesc": True, "state": None,
            "vulnerability": {"kind": "cve", "cveId": LOG4SHELL},
        })],
        ("findingScore.listScoredFindings",),
        ("Findings (whole tenant; page 1, showing 3 of 37):",),
    ),
    "get_opencrq_cve_finding": Read(
        "get_opencrq_cve_finding", {"cve_id": LOG4SHELL},
        # finding-score/finding-score-router.ts: getUniqueCveFinding, estateInput + cveId
        [_sent("findingScore.getUniqueCveFinding", {"cveId": LOG4SHELL})],
        ("findingScore.getUniqueCveFinding",),
        (
            "[CVE finding] CVE-2021-44228 (Apache Log4j2 JNDI Remote Code Execution (Log4Shell))",
            "Estate: whole tenant", "Score: 52 (medium)", "Affected assets: 3",
            "Known exploited (CISA KEV): yes", "EPSS: 0.976", "First seen: 2025-11-13",
        ),
    ),
    "get_opencrq_asset_posture": Read(
        "get_opencrq_asset_posture", {"supporting_asset_id": TRADING_PLATFORM},
        [
            # finding-score/finding-score-router.ts: getAssetPosture input is {supportingAssetId}
            _sent("findingScore.getAssetPosture", {"supportingAssetId": TRADING_PLATFORM}),
            # listAssetFindings: pagedInput + supportingAssetId, state defaulting to applicable
            _sent("findingScore.listAssetFindings", {
                "supportingAssetId": TRADING_PLATFORM, "page": 1, "limit": 10, "sortBy": "score",
            }),
        ],
        ("findingScore.getAssetPosture",),
        (
            f"[Asset posture] Trading Platform (Murex) (id: {TRADING_PLATFORM})",
            "Posture score: 11/100",
            "CVE findings by exploitability band: critical 0, high 1, medium 0, low 0",
            "Worst finding: CVE-2021-44228, score 52 (exploitability 89, impact 31)",
            "Findings, worst first (page 1, showing 2 of 2):", "CVE-2024-6387", "score unscored",
        ),
        overrides={
            "findingScore.listAssetFindings": _fixture("findingScore.listAssetFindings.posture")[1],
        },
    ),
    "get_opencrq_technique_test_posture": Read(
        "get_opencrq_technique_test_posture", {},
        # finding-score/finding-score-router.ts: getTechniqueTestPosture takes estateInput
        [_sent("findingScore.getTechniqueTestPosture", {})],
        ("findingScore.getTechniqueTestPosture",),
        (
            "Attack simulation results on 3 tested assets (whole tenant):",
            "Prevention: 5 of 9 results held (56%)", "Detection: 5 of 10 results held (50%)",
        ),
    ),
    "list_opencrq_threats": Read(
        "list_opencrq_threats", {"limit": 3},
        # threats/threats-router.ts: list, pagedInput(THREAT_LIST_SORT_KEYS) + filters
        [_sent("threats.list", {"page": 1, "limit": 3, "sortBy": "outcomeScore", "sortDesc": True})],
        ("threats.list",),
        (
            "Threats (page 1, showing 3 of 16):",
            # no observed activity: no score, no date (lastActivityAt is an authoring date)
            "Sandworm Team (G0034, intrusion-set): activity unknown; best outcome match "
            "Destructive Attack (97%); 1 CVEs, 5 techniques",
            "Salt Typhoon (G1045, intrusion-set): activity 100%, last observed 2026-10-10",
        ),
    ),
    "get_opencrq_threat": Read(
        "get_opencrq_threat", {"threat_id": SANDWORM},
        [
            # threats/threats-router.ts: getDetail input is {id}, getOutcomeScoresForAnalysis {analysisId}
            _sent("threats.getDetail", {"id": SANDWORM}),
            _sent("threats.getOutcomeScoresForAnalysis", {"analysisId": SANDWORM}),
        ],
        ("threats.getDetail", "threats.getOutcomeScoresForAnalysis"),
        (
            "[Threat] Sandworm Team (G0034, intrusion-set)",
            f"{BASE}/{TENANT}/threat-center/threats/{SANDWORM}/overview",
            "Actor class: state sponsored", "First seen: 2013-08-23",
            "Goals: Disrupt regional power grid operations for as long as possible",
            "Targeted locations: Germany, France (matching your organizations: Germany, France)",
            "Exploited CVEs (1): CVE-2020-1472", "T1485 Data Destruction",
            "Destructive Attack: 97% (5 matching techniques)",
        ),
    ),
    "list_opencrq_scenario_threats": Read(
        "list_opencrq_scenario_threats", {"risk_scenario_id": SCENARIO},
        # threats/threats-router.ts: getRelevantForScenario input is {scenarioId}
        [_sent("threats.getRelevantForScenario", {"scenarioId": SCENARIO})],
        ("threats.getRelevantForScenario",),
        (
            "Threats relevant to the risk scenario (3), by threat score",
            # ranked on relevance x activity weight, as OpenCRQ's front ranks them: the active
            # actor comes before the more relevant ones nobody observed, which keep a low score
            "risk scenario (3), by threat score (relevance x activity, out of 100):\n"
            "- Salt Typhoon (G1045, intrusion-set): threat score 87, relevance 87%, matches "
            "attack outcome, country France, sector Finance; activity 100%, last observed 2026-10-10",
            "APT29 (Cozy Bear) (G0016, intrusion-set): threat score 2, relevance 89%",
            "activity unknown (id: 01a139e7-7dcb-7c37-b3a7-768c95bb6664)",
        ),
    ),
    "list_opencrq_cve_threats": Read(
        "list_opencrq_cve_threats", {"cve_id": LOG4SHELL},
        # threats/threats-router.ts: getByCveId input is {cveId}
        [_sent("threats.getByCveId", {"cveId": LOG4SHELL})],
        ("threats.getByCveId",),
        (
            "Threats citing CVE-2021-44228 (3, 1 active):",
            "ALPHV (BlackCat) (intrusion-set): activity 97%, last observed 2026-10-05",
        ),
    ),
    "list_opencrq_treatment_plans": Read(
        "list_opencrq_treatment_plans", {},
        # treatment-plans/treatment-plans-router.ts: getTreatmentPlans, {organizationId?}
        [_sent("treatmentPlans.getTreatmentPlans", {})],
        ("treatmentPlans.getTreatmentPlans",),
        (
            "Risk treatment plans (whole tenant, 2):",
            "Widen endpoint coverage [draft] in Acme Bank Europe: reduces ALE by 9,051 USD/yr, "
            "year-1 cost 235,000 USD, return -96%",
            f"{BASE}/{TENANT}/treatment-plans/{PLAN}",
        ),
    ),
    "get_opencrq_treatment_plan": Read(
        "get_opencrq_treatment_plan", {"treatment_plan_id": PLAN},
        [
            # treatment-plans/treatment-plans-router.ts: getTreatmentPlan {id}, then {planId}
            _sent("treatmentPlans.getTreatmentPlan", {"id": PLAN}),
            _sent("treatmentPlans.getTreatmentPlanProjection", {"planId": PLAN}),
            _sent("treatmentPlans.getTreatmentPlanCandidates", {"planId": PLAN}),
        ],
        (
            "treatmentPlans.getTreatmentPlan", "treatmentPlans.getTreatmentPlanProjection",
            "treatmentPlans.getTreatmentPlanCandidates",
        ),
        (
            "[Treatment plan] Widen endpoint coverage", "Owner: Bob Johnson",
            "Cost: 95,000 USD one-off, 140,000 USD/yr recurring",
            "IDPS Detection and Prevention Systems: from strength L1, coverage L1 to coverage L2",
            "IAM Identity and Access Management: from strength L2, coverage L2 to coverage L3",
            "Simulated ALE reduction: 9,051 USD/yr over 1 of 2 risk scenarios priced",
            "Business Interruption on Internet Banking Platform: ALE 129,337 USD/yr to "
            "120,286 USD/yr, reduction 9,051 USD/yr (P10 7,381 USD, P90 13,669 USD)",
            "Data Breach on AML/KYC Screening Platform: not priced (incomplete)",
        ),
    ),
    "list_opencrq_budget_reviews": Read(
        "list_opencrq_budget_reviews", {},
        # budget-reviews/budget-reviews-router.ts: getBudgetReviews, {organizationId?}
        [_sent("budgetReviews.getBudgetReviews", {})],
        ("budgetReviews.getBudgetReviews",),
        (
            # budgetReviews prices loss avoided, not an ALE drop
            "Exercice 2025-2026 [open] in Acme Bank, 2025-11-01 to 2026-10-31: spent "
            "4,000,000 USD on 3 lines, loss avoided 14,970,226 USD/yr, return 274%",
        ),
    ),
    "get_opencrq_budget_review": Read(
        "get_opencrq_budget_review", {"budget_review_id": REVIEW},
        [
            # budget-reviews/budget-reviews-router.ts: getBudgetReview {id}, getBudgetReviewFigures {reviewId}
            _sent("budgetReviews.getBudgetReview", {"id": REVIEW}),
            _sent("budgetReviews.getBudgetReviewFigures", {"reviewId": REVIEW}),
        ],
        ("budgetReviews.getBudgetReview", "budgetReviews.getBudgetReviewFigures"),
        (
            "[Budget review] Exercice 2025-2026", "Owner: Alice Martin",
            "Identity hardening project (build): 900,000 USD, amortized over 3 years, CSF PR/DE, "
            "2 controls",
            "line Identity hardening project: spent 900,000 USD, loss avoided 2,256,048 USD/yr "
            "(P10 958,486 USD, P90 3,740,987 USD), return 151% (amortized 652%)",
            # a run line spends its whole amount in the year: no amortized return
            "line SOC and endpoint licences: spent 2,600,000 USD, loss avoided 11,604,420 USD/yr "
            "(P10 5,343,005 USD, P90 18,993,244 USD), return 346%\n",
            "ensemble: spent 4,000,000 USD, loss avoided 14,970,226 USD/yr",
        ),
    ),
    "get_opencrq_control_validation": Read(
        "get_opencrq_control_validation", {"control_id": EDR},
        [
            # controls/capability/control-capabilities-router.ts: getControlCapability {id}
            _sent("controlCapabilities.getControlCapability", {"id": EDR}),
            # control-validation/control-validation-router.ts: {capabilityId, organizationId nullish}
            _sent("controlValidation.getControlValidation", {"capabilityId": EDR}),
            _sent("controlValidation.getControlValidationEvidence", {"capabilityId": EDR}),
        ],
        ("controlValidation.getControlValidation", "controlValidation.getControlValidationEvidence"),
        (
            f"[Control validation] EDR Endpoint Detection and Response (id: {EDR}, whole tenant)",
            "Techniques tested: 3, validated: 1, contradicted: 2", "Latest proof: 2026-10-08",
            "Contradicted technique checks (3): T1190 detect (1/2 votes)",
            "Validated technique checks (1): T1003 prevent (1/1 votes)",
            "T1190 detect: contradicted in Web exploitation simulation (EDR, 2026-10-07) on "
            "Card Authorization Engine",
        ),
        overrides={
            "controlCapabilities.getControlCapability":
                _fixture("controlCapabilities.getControlCapability.edr")[1],
        },
    ),
    "get_opencrq_risk_dashboard": Read(
        "get_opencrq_risk_dashboard", {},
        [
            # dashboard/dashboard-router.ts: getScenarioSummaries takes WindowedDashboardInput,
            # the others DashboardInput, organizationId optional and not nullable
            _sent("dashboard.getScenarioSummaries", {"windowDays": 90}),
            _sent("dashboard.getToleranceGaps", {}),
            _sent("dashboard.getTopControls", {}),
            _sent("dashboard.getMostActiveThreats", {}),
        ],
        (
            "dashboard.getScenarioSummaries", "dashboard.getToleranceGaps",
            "dashboard.getTopControls", "dashboard.getMostActiveThreats",
        ),
        (
            "Total exposure: 630,733 USD/yr over 2 priced of 3 risk scenarios.",
            # deltaPct is a ratio: -0.464 is a 46% drop
            "Espionage / Intellectual Property Loss on Proprietary Trading Algorithms (Acme Wealth "
            "Management): ALE 485,762 USD/yr (P10 64,709 USD, P90 1,131,594 USD), -46% over the "
            "window",
            "Data Breach on AML/KYC Screening Platform (Acme Bank Europe): not simulated yet",
            "Data Breach on Retail Lending Platform: 52,797,958 USD loss at 10% annual "
            "probability, above the 50,000,000 USD the scenario tolerance accepts",
            "1 scenarios have no risk tolerance set.",
            "DLP Data Loss Prevention: improve coverage, ALE reduction "
            "4,655,896 USD/yr across 6 scenarios",
            # the recorded dashboard row has no lastObservedAt
            "Salt Typhoon (intrusion-set): activity unknown, relevant to 7 scenarios",
        ),
    ),
    "list_opencrq_connectors": Read(
        "list_opencrq_connectors", {},
        # connectors/connectors-router.ts: listConnectors, no input
        [_sent("connectors.listConnectors")],
        ("connectors.listConnectors",),
        (
            "nist (vulnerability, scheduled_pull): never_synced "
            "(configured, no sync has finished yet); schedule daily, "
            "next 2026-10-15T06:00:00.000Z; feeds cves",
            f"{BASE}/{TENANT}/integrations/connectors/nist/overview",
        ),
    ),
    "list_opencrq_connector_syncs_stream": Read(
        "list_opencrq_connector_syncs", {"source": "opencti"},
        # connectors/connectors-router.ts: getStreamState answers the live stream, listRuns refuses it
        [_sent("connectors.getStreamState", {"source": "opencti"})],
        ("connectors.getStreamState",),
        ("opencti live stream: not_configured", "Reached the live tail: no"),
    ),
    "list_opencrq_connector_syncs_stream_missing_capability": Read(
        "list_opencrq_connector_syncs",
        {"source": "opencti"},
        # getStreamState.missingCapability: the capability the OpenCTI token lacks (#1797)
        [_sent("connectors.getStreamState", {"source": "opencti"})],
        ("connectors.getStreamState",),
        ("Missing capability: the OpenCTI token lacks the TAXIIAPI_SETCOLLECTIONS capability to create the stream",),
        overrides={
            "connectors.getStreamState": {
                **_fixture("connectors.getStreamState")[1],
                "missingCapability": "TAXIIAPI_SETCOLLECTIONS",
            },
        },
    ),
    "list_opencrq_connectors_stream_missing_capability": Read(
        "list_opencrq_connectors",
        {},
        # listConnectors lacks the capability: one getStreamState read adds it to the opencti row (#1797)
        [_sent("connectors.listConnectors"), _sent("connectors.getStreamState", {"source": "opencti"})],
        ("connectors.listConnectors",),
        (
            "- opencti (threat_intel, live_stream): not_configured (no live stream and service "
            "account selected in this tenant; the OpenCTI token lacks the TAXIIAPI_SETCOLLECTIONS "
            "capability to create the stream)",
        ),
        overrides={
            "connectors.listConnectors": [
                {
                    "source": "opencti",
                    "domain": "threat_intel",
                    "mechanism": "live_stream",
                    "state": "not_configured",
                    "tenantConfigured": False,
                },
            ],
            "connectors.getStreamState": {
                **_fixture("connectors.getStreamState")[1],
                "missingCapability": "TAXIIAPI_SETCOLLECTIONS",
            },
        },
    ),
}


@pytest.mark.parametrize("case", READS.values(), ids=READS.keys())
async def test_read_contract(case: Read):
    answers = {
        **dict(_fixture(name) for name in case.fixtures),
        **case.overrides,
    }
    with _recorded_trpc(answers) as calls:
        out = await _run(case.tool, **case.args)
    assert calls == case.calls
    for text in case.expect:
        assert text in out, f"{text!r} missing from:\n{out}"
    _assert_no_placeholder_for_a_value(out, answers)


# -- Behaviours the recorded responses do not show ------------------------------


@pytest.mark.parametrize(
    ("name", "args", "expected"),
    [
        ("list_opencrq_threats", {"entity_type": "__bad__"}, "entity_type"),
        ("list_opencrq_threats", {"sort_by": "__bad__"}, "sort_by"),
        ("list_opencrq_threats", {"limit": 500}, "limit"),
        ("get_opencrq_threat", {"threat_id": " "}, "threat_id is required"),
        ("list_opencrq_scenario_threats", {}, "risk_scenario_id is required"),
        ("list_opencrq_cve_threats", {"cve_id": "log4shell"}, "CVE identifier"),
        ("list_opencrq_findings", {"state": "__bad__"}, "state"),
        ("list_opencrq_findings", {"cve_id": "CVE-24-1"}, "CVE identifier"),
        ("list_opencrq_findings", {"page": 0}, "page"),
        ("get_opencrq_cve_finding", {"cve_id": ""}, "CVE identifier"),
        ("get_opencrq_asset_posture", {}, "supporting_asset_id is required"),
        ("get_opencrq_treatment_plan", {}, "treatment_plan_id is required"),
        ("get_opencrq_budget_review", {}, "budget_review_id is required"),
        ("get_opencrq_control_validation", {}, "control_id is required"),
        ("get_opencrq_risk_dashboard", {"window_days": 0}, "window_days"),
        ("list_opencrq_connector_syncs", {"source": "jira"}, "source must be"),
    ],
)
async def test_invalid_arguments_return_before_any_call(name, args, expected):
    with _recorded_trpc({}) as calls:
        out = await _run(name, **args)
    assert expected in out
    assert calls == []


async def test_a_closed_estate_names_its_mapped_assets():
    _, organization = _fixture("organizations.getOrganization")
    closed = dict(organization, estateMode="closed", mappedAssetCount=12)
    with _recorded_trpc({
        "organizations.getOrganization": closed,
        "findingScore.getTopVulnerabilities": _fixture("findingScore.getTopVulnerabilities")[1],
    }):
        out = await _run("get_opencrq_top_vulnerabilities", organization_id=ORGANIZATION)
    assert "Top 3 vulnerabilities by finding score (Acme Bank, its 12 mapped assets):" in out
    # A CVE finding is read per organization, so its link opens under the one asked for.
    assert f"{BASE}/{TENANT}/{ORGANIZATION}/findings/cve/CVE-2021-44228/overview" in out


async def test_an_unknown_organization_stops_a_findings_read():
    with _recorded_trpc({"organizations.getOrganization": None}) as calls:
        out = await _run("list_opencrq_findings", organization_id=ORGANIZATION)
    assert out == f"Organization {ORGANIZATION} not found."
    assert [c["procedure"] for c in calls] == ["organizations.getOrganization"]


async def test_a_failed_dashboard_section_is_named_and_the_rest_still_shown():
    answers = {
        **dict(_fixture(name) for name in (
            "dashboard.getScenarioSummaries", "dashboard.getToleranceGaps", "dashboard.getTopControls",
        )),
        "dashboard.getMostActiveThreats": (
            "Error: OpenCRQ server error (500) while trying to get most active threats."
        ),
    }
    with _recorded_trpc(answers):
        out = await _run("get_opencrq_risk_dashboard")
    assert "Total exposure: 630,733 USD/yr" in out
    assert "(Active threats unavailable: Error: OpenCRQ server error (500)" in out


async def test_a_control_no_simulation_tested_is_declared_not_proven():
    with _recorded_trpc({
        "controlCapabilities.getControlCapability":
            _fixture("controlCapabilities.getControlCapability.edr")[1],
        "controlValidation.getControlValidation": {"summary": {"testedTechniques": 0}, "cells": []},
        "controlValidation.getControlValidationEvidence": [],
    }):
        out = await _run("get_opencrq_control_validation", control_id=EDR)
    assert "its posture is declared, not proven" in out


async def test_a_control_that_cannot_be_read_is_not_reported_as_untested():
    with _recorded_trpc({"controlCapabilities.getControlCapability": None}) as calls:
        missing = await _run("get_opencrq_control_validation", control_id=EDR)
    assert missing == f"Control {EDR} not found."
    assert [c["procedure"] for c in calls] == ["controlCapabilities.getControlCapability"]
    refused = "Error: OpenCRQ server error (500) while trying to get control."
    with _recorded_trpc({"controlCapabilities.getControlCapability": refused}):
        result = await _execute("get_opencrq_control_validation", control_id=EDR)
    assert result.is_error and result.content == refused


async def test_connector_syncs_list_a_job_connector_runs():
    runs = {
        "inProgress": [],
        "completed": [{
            "id": "r-1", "status": "succeeded", "enqueuedAt": "2026-10-14T06:00:00.000Z",
            "finishedAt": "2026-10-14T06:01:10.000Z", "processedTotal": 10, "expectedTotal": 10,
            "triggeredBy": "schedule",
        }],
        "inProgressCount": 0,
        "completedCount": 31,
    }
    with _recorded_trpc({"connectors.listRuns": runs}) as calls:
        out = await _run("list_opencrq_connector_syncs", source="NIST")
    assert calls == [_sent("connectors.listRuns", {"source": "nist"})]
    assert "Completed (31, latest first):" in out
    assert (
        "succeeded, enqueued 2026-10-14T06:00:00.000Z, finished 2026-10-14T06:01:10.000Z, "
        "10/10 processed, triggered by schedule"
    ) in out


async def test_a_whole_list_pages_with_an_offset():
    _, rows = _fixture("threats.getByCveId")
    many = [dict(rows[i % len(rows)], name=f"Actor {i}") for i in range(32)]
    with _recorded_trpc({"threats.getByCveId": many}):
        first = await _run("list_opencrq_cve_threats", cve_id=LOG4SHELL)
        rest = await _run("list_opencrq_cve_threats", cve_id=LOG4SHELL, offset=30)
    assert "Actor 29 " in first and "Actor 30 " not in first
    assert "Showing 1-30 of 32; call again with offset=30 for the next ones." in first
    assert "Actor 30 " in rest and "Showing 31-32 of 32." in rest


def _threat(name: str, observed: str | None, **extra) -> dict:
    return {
        "id": name, "name": name, "entityType": "intrusion-set",
        "activityScore": 0.5, "lastObservedAt": observed, **extra,
    }


def _days_ago(days: int) -> str:
    return (NOW - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def test_a_dated_threat_prints_its_score_and_date_and_an_undated_one_is_unknown():
    dated = _threat("A", "2026-10-31T09:42:02.845Z", activityScore=0.4)
    assert threats._fmt_activity(dated) == "activity 40%, last observed 2026-10-31"
    for undated in ({}, {"lastObservedAt": None}, {"lastObservedAt": ""}):
        assert threats._fmt_activity({**undated, "activityScore": 0.7}) == "activity unknown"


async def _scenario_threats(items: list[dict]) -> str:
    with _recorded_trpc({"threats.getRelevantForScenario": {"items": items}}):
        return await _run("list_opencrq_scenario_threats", risk_scenario_id=SCENARIO)


def _relevant(name: str, relevance: float, score: float | None, weight: float, **extra) -> dict:
    row = _threat(name, None if score is None else "2026-10-31T09:42:02.845Z")
    row.update(combinedRelevance=relevance, activityScore=score, activityWeight=weight, **extra)
    return row


async def test_an_undated_threat_ranks_after_the_dated_ones_with_a_low_non_zero_score():
    out = await _scenario_threats([
        _relevant("Undated", 0.9, None, 0.021),
        _relevant("Dated", 0.2, 0.5, 0.5),
    ])
    assert out.index("- Dated ") < out.index("- Undated ")
    assert "- Undated (intrusion-set): threat score 2, relevance 90%, matches none; activity unknown" in out
    assert "- Dated (intrusion-set): threat score 10," in out


async def test_a_threat_ranks_on_the_unrounded_product_and_ties_keep_relevance_order():
    out = await _scenario_threats([
        _relevant("Low", 0.5, None, 0.021),
        _relevant("High", 0.9, None, 0.021),
        # both round to 2, but 0.9 x 0.0225 outweighs 0.95 x 0.0205
        _relevant("Heavier", 0.9, None, 0.0225),
        _relevant("Lighter", 0.95, None, 0.0205),
    ])
    names = [line.split(" (")[0] for line in out.splitlines() if line.startswith("- ")]
    assert names == ["- Heavier", "- Lighter", "- High", "- Low"]
    assert threats._threat_score(_relevant("Heavier", 0.9, None, 0.0225)) == 2
    assert threats._threat_score(_relevant("Lighter", 0.95, None, 0.0205)) == 2


async def test_two_undated_threats_of_the_same_weight_follow_their_relevance():
    out = await _scenario_threats([
        _relevant("Less", 0.4, None, 0.021),
        _relevant("More", 0.8, None, 0.021),
    ])
    assert out.index("- More ") < out.index("- Less ")


async def test_a_status_the_date_contradicts_is_not_read():
    stale = _threat("Stale", _days_ago(400), activityStatus="active")
    fresh = _threat("Fresh", _days_ago(1), activityStatus="dormant")
    with _recorded_trpc({"threats.getByCveId": [stale, fresh]}):
        out = await _run("list_opencrq_cve_threats", cve_id=LOG4SHELL)
    assert "(2, 1 active)" in out
    assert "activity active" not in out and "dormant" not in out


@pytest.mark.parametrize(
    ("observed", "active"),
    [
        (_days_ago(89), 1),
        (_days_ago(91), 0),
        ((NOW + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ"), 1),
        ("2026-10-31", 1),
        (None, 0),
        ("", 0),
        ("last tuesday", 0),
        (20261031, 0),
    ],
    ids=["89-days", "91-days", "future", "date-only", "none", "empty", "unparseable", "not-a-string"],
)
async def test_a_cve_counts_as_active_the_threats_observed_within_90_days(observed, active):
    with _recorded_trpc({"threats.getByCveId": [_threat("A", observed)]}):
        out = await _run("list_opencrq_cve_threats", cve_id=LOG4SHELL)
    assert f"(1, {active} active)" in out


async def test_a_page_past_the_end_says_so():
    with _recorded_trpc({"findingScore.listScoredFindings": {"items": [], "total": 37}}):
        out = await _run("list_opencrq_findings", page=9)
    assert out == "Page 9 is past the end: 37 in total."


async def test_a_client_sending_null_or_a_string_number_gets_an_answer():
    with _recorded_trpc({"threats.list": _fixture("threats.list")[1]}) as calls:
        out = await _run(
            "list_opencrq_threats", search=None, attack_outcome_id=None, page="1", limit="3"
        )
    assert "Threats (page 1, showing 3 of 16):" in out
    assert calls[0]["input"] == {"page": 1, "limit": 3, "sortBy": "outcomeScore", "sortDesc": True}
    with _recorded_trpc({}) as calls:
        out = await _run("list_opencrq_findings", page="two")
    assert out == "page must be an integer >= 1."
    assert calls == []


async def test_names_sort_a_to_z():
    with _recorded_trpc({"threats.list": _fixture("threats.list")[1]}) as calls:
        await _run("list_opencrq_threats", sort_by="name")
    assert calls[0]["input"]["sortDesc"] is False


async def test_an_outcome_filtered_list_names_the_match_with_that_outcome():
    with _recorded_trpc({"threats.list": _fixture("threats.list")[1]}):
        out = await _run("list_opencrq_threats", attack_outcome_id=SCENARIO)
    assert "Threats matching the attack outcome (page 1" in out
    assert "; match with Destructive Attack (97%)" in out
    assert "best outcome match" not in out


async def test_an_organization_must_be_a_uuid():
    with _recorded_trpc({}) as calls:
        out = await _run("list_opencrq_budget_reviews", organization_id="Acme Bank")
    assert out == "organization_id must be an organization UUID, got 'Acme Bank'."
    assert calls == []


async def test_an_unreported_estate_mode_claims_no_estate():
    _, organization = _fixture("organizations.getOrganization")
    with _recorded_trpc({
        "organizations.getOrganization": {k: v for k, v in organization.items() if k != "estateMode"},
        "findingScore.getTechniqueTestPosture": _fixture("findingScore.getTechniqueTestPosture")[1],
    }):
        out = await _run("get_opencrq_technique_test_posture", organization_id=ORGANIZATION)
    assert "(Acme Bank, estate not reported by OpenCRQ):" in out


async def test_an_organization_dashboard_names_it_and_links_under_it():
    _, organization = _fixture("organizations.getOrganization")
    scenarios = [
        {"name": "Unmapped", "noMappedAsset": True, "latestRun": {"aleMean": 9_000_000}},
        {"name": "Steady", "latestRun": {"aleMean": 100_000}, "trend": {"deltaPct": 0.004}},
    ]
    with _recorded_trpc({
        "organizations.getOrganization": organization,
        "dashboard.getScenarioSummaries": scenarios,
    }) as calls:
        out = await _run("get_opencrq_risk_dashboard", organization_id=ORGANIZATION)
    assert calls[1] == _sent(
        "dashboard.getScenarioSummaries", {"windowDays": 90, "organizationId": ORGANIZATION}
    )
    assert "Risk dashboard (Acme Bank, last 90 days):" in out
    assert f"{BASE}/{TENANT}/{ORGANIZATION}/dashboards/risk)" in out
    # OpenCRQ keeps an unmapped scenario's last run on the row but counts it as not priced
    assert "Total exposure: 100,000 USD/yr over 1 priced of 2 risk scenarios." in out
    assert out.index("- Steady") < out.index("- Unmapped: no asset mapped, not priced")
    assert "Steady: ALE 100,000 USD/yr, stable over the window" in out


async def test_a_partly_priced_budget_figure_says_so():
    _, reviews = _fixture("budgetReviews.getBudgetReviews")
    partial = [dict(reviews[0], state="not-priced")]
    with _recorded_trpc({"budgetReviews.getBudgetReviews": partial}):
        out = await _run("list_opencrq_budget_reviews")
    assert "return 274% (partial: some risk scenarios are not priced yet)" in out


async def test_a_whole_list_of_plans_pages_with_an_offset():
    _, plans = _fixture("treatmentPlans.getTreatmentPlans")
    many = [dict(plans[i % len(plans)], name=f"Plan {i}") for i in range(23)]
    with _recorded_trpc({"treatmentPlans.getTreatmentPlans": many}):
        out = await _run("list_opencrq_treatment_plans", offset=20)
    assert "Plan 20 " in out and "Plan 19 " not in out
    assert "Showing 21-23 of 23." in out


def test_a_link_takes_only_plain_path_segments():
    assert _grc_link(BASE, TENANT, "control_validation", "../settings") == ""
    assert _grc_link(BASE, TENANT, "threat", "x) [y](https://evil.example") == ""
    assert _grc_link(BASE, "../t", "threat", SANDWORM) == ""
    assert _grc_link(BASE, TENANT, "dashboard", "risk", organization_id="a/b") == ""
    assert _grc_scenario_link(BASE, TENANT, ORGANIZATION, "../settings") == ""
    assert _grc_scenario_link(BASE, TENANT, "x) [y](https://evil.example", SANDWORM) == ""
    assert _grc_primary_asset_link(BASE, "../t", ORGANIZATION, SANDWORM) == ""
    assert _grc_primary_asset_link(BASE, TENANT, ORGANIZATION, "a/b") == ""
    assert _grc_link(BASE, TENANT, "cve_finding", "CVE-2021-44228").endswith(
        f"{BASE}/{TENANT}/findings/cve/CVE-2021-44228/overview)"
    )


def test_new_entity_links():
    assert _grc_link(BASE, TENANT, "connector", "microsoft-graph") == (
        f"  Link: [Open in OpenCRQ]({BASE}/{TENANT}/integrations/connectors/microsoft-graph/overview)"
    )
    assert _grc_link(BASE, TENANT, "dashboard", "risk").endswith(f"{BASE}/{TENANT}/dashboards/risk)")


@pytest.mark.parametrize(("value", "shown"), [(0.1, "10%"), (0.001, "0.1%"), (0.995, "99.5%"), (1, "100%")])
def test_a_probability_reads_as_a_plain_percentage(value, shown):
    assert dashboard._fmt_probability(value) == shown


_USER_SESSION_PREFIXES = (
    "findingScore.",
    "threats.getDetail",
    "threats.getRelevantForScenario",
    "threats.getByCveId",
    "dashboard.getMostActiveThreats",
    "controlValidation.",
)
_USER_SESSION_READS = {
    name: next(p for p in case.fixtures if p.startswith(_USER_SESSION_PREFIXES))
    for name, case in READS.items()
    if any(p.startswith(_USER_SESSION_PREFIXES) for p in case.fixtures)
}


@pytest.mark.parametrize(("name", "procedure"), _USER_SESSION_READS.items(), ids=_USER_SESSION_READS.keys())
async def test_a_user_session_read_refused_with_a_500_says_it_needs_a_user_session(name, procedure):
    case = READS[name]
    answers = {
        **dict(_fixture(fixture) for fixture in case.fixtures),
        **case.overrides,
        procedure: opencrq_common.TrpcError("Error: OpenCRQ server error (500).", None, 500),
    }
    with _recorded_trpc(answers):
        result = await _execute(case.tool, **case.args)
    assert "it needs a user session" in result.content


async def test_a_user_session_read_failing_otherwise_is_not_blamed_on_the_token():
    refused = opencrq_common.TrpcError("Error: OpenCRQ refused the request (404).", None, 404)
    with _recorded_trpc({"threats.getDetail": refused}):
        result = await _execute("get_opencrq_threat", **READS["get_opencrq_threat"].args)
    assert result.content.startswith("Error: OpenCRQ refused the request (404).")
    assert "user session" not in result.content
