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

"""Built-in fleets: the fleets the platform seeds by slug (#8988).

Each entry is seeded by :func:`app.agents.fleet_seed.seed_builtin_fleets` on
every boot, in the modes its ``platform_targets`` name
(:func:`app.agents.seed.seed_matches_platform`, xtm_one and dev without any)
and never in copilot mode. The contract is the one of ``BUILTIN_AGENTS``:
company-managed, ``is_builtin``, upserted idempotently, the stale ones swept,
user fleets never touched.

Shape of an entry (every agent named by its built-in slug; a member whose
agent is not seeded in this deployment, feature-gated or of another mode, is
left out and its reports move up to its own parent)::

    {
        "slug": "ctem-threat-response",
        "name": "Threat-informed exposure response",
        "description": "...",
        "icon": "Crosshair",
        "platform_targets": ["xtm_one", "dev"],      # optional
        "orchestrator": "ctem-threat-response-orchestrator",
        "members": [
            {"agent": "ctem-threat-response-orchestrator", "role_label": "Orchestrator"},
            {"agent": "ctem-risk-quantifier", "role_label": "Risk",
             "parent": "ctem-threat-response-orchestrator", "exposed": False},
        ],
        "connected": [
            {"agent": "deep-investigation-agent",
             "collaboration_instruction": "Consulted for open-source context"},
        ],
        # Agents outside the fleet given a handover to its orchestrator
        # (the CTEM Assistant is the entry point of every CTEM fleet).
        "entry_handovers": [{"from": "ctem-assistant", "type": "delegate"}],
    }

Assignments stay where they are declared: ``BUILTIN_ASSIGNMENTS`` on the member
agents (event, work_item and schedule triggers), seeded before the fleets.
The CTEM fleets F1 to F4 (``dev-docs/ctem-fleets.md``) land here, one slice each.
"""

from __future__ import annotations

from app.agents.ctem_ai_security import AI_SECURITY_FLEET
from app.agents.ctem_third_party_risk import THIRD_PARTY_RISK_FLEET
from app.agents.openaev_detection_desk import DETECTION_DESK_FLEET
from app.agents.opencrq_risk_budget_desk import RISK_BUDGET_DESK_FLEET
from app.agents.opencti_pir_desk import PIR_DESK_FLEET

_F1 = "ctem-threat-response-orchestrator"

#: F1 Threat-informed exposure response (dev-docs/ctem-fleets.md s.4.1, #9114).
CTEM_THREAT_RESPONSE: dict = {
    "slug": "ctem-threat-response",
    "name": "Threat-informed exposure response",
    "description": (
        "A published threat becomes an approved plan in days: relevance against "
        "the organization profile, an OpenAEV scenario proposed, the expected "
        "loss in OpenCRQ, three actions of the week, each fix proven by a "
        "re-test and priced in loss avoided."
    ),
    "icon": "Crosshair",
    "orchestrator": _F1,
    "members": [
        {"agent": _F1, "role_label": "Orchestrator"},
        {"agent": "ctem-threat-relevance-analyst", "role_label": "Relevance", "parent": _F1, "exposed": False},
        {"agent": "openaev-scenario-creator", "role_label": "Validation", "parent": _F1, "exposed": False},
        {"agent": "ctem-risk-quantifier", "role_label": "Risk", "parent": _F1, "exposed": False},
        {"agent": "ctem-remediation-coordinator", "role_label": "Mobilization", "parent": _F1, "exposed": False},
        {
            "agent": "opencti-threat-hunt-creator",
            "role_label": "Hunt",
            "parent": "ctem-threat-relevance-analyst",
            "exposed": False,
        },
        {
            "agent": "opencti-detection-rule-converter",
            "role_label": "Detections",
            "parent": "ctem-remediation-coordinator",
            "exposed": False,
        },
    ],
    "connected": [
        {
            "agent": "ctem-assistant",
            "collaboration_instruction": "Entry point; delegates 'are we at risk' questions",
        },
        {
            "agent": "deep-investigation-agent",
            "collaboration_instruction": "Consulted for open-source context on a threat",
        },
    ],
    "entry_handovers": [{"from": "ctem-assistant", "type": "delegate"}],
}

#: F4 CTEM posture and reporting (#9112, ``dev-docs/ctem-fleets.md`` section
#: 4.4): the five stage analysts and the Exposure Briefing Analyst folded under
#: one orchestrator. Their weekly and monthly assignments keep their names and
#: stay where they are declared, as the refresh floor.
CTEM_POSTURE_FLEET: dict = {
    "slug": "ctem-posture",
    "name": "CTEM posture and reporting",
    "description": (
        "Keeps the CTEM posture current: the five stage assessments, the "
        "exposure briefing and the board report, across OpenCTI, OpenAEV and "
        "OpenCRQ."
    ),
    "icon": "Gauge",
    "platform_targets": ["xtm_one", "dev"],
    "orchestrator": "ctem-posture-orchestrator",
    "members": [
        {"agent": "ctem-posture-orchestrator", "role_label": "Orchestrator", "exposed": True},
        *(
            {"agent": slug, "role_label": role, "parent": "ctem-posture-orchestrator", "exposed": False}
            for slug, role in (
                ("ctem-scoping-analyst", "Scoping"),
                ("ctem-discovery-analyst", "Discovery"),
                ("ctem-prioritization-analyst", "Prioritization"),
                ("ctem-validation-analyst", "Validation"),
                ("ctem-mobilization-analyst", "Mobilization"),
                ("exposure-briefing-analyst", "Report"),
            )
        ),
    ],
    "connected": [
        {
            "agent": "opencti-dashboard-widget-creator",
            "collaboration_instruction": "Consulted to turn a posture finding into an OpenCTI dashboard widget",
        },
    ],
    "entry_handovers": [{"from": "ctem-assistant", "type": "delegate"}],
}

BUILTIN_FLEETS: list[dict] = [CTEM_THREAT_RESPONSE, AI_SECURITY_FLEET, CTEM_POSTURE_FLEET]

#: F2, vulnerability prioritization to verified fix (s.4.2, #9120). An agent
#: has one home fleet: the CTEM role agents shared with F1 and F3 (risk
#: quantifier, remediation coordinator) and the module specialists F1 folds
#: are CONNECTED here, never members, so F2 never moves them.
CTEM_VULN_PRIORITIZATION_FLEET: dict = {
    "slug": "ctem-vuln-prioritization",
    "name": "Vulnerability prioritization to verified fix",
    "description": (
        "Which CVEs do we fix on Monday: KEV, EPSS jumps and sightings matched to "
        "the organization's assets, validated in OpenAEV, quantified in OpenCRQ, "
        "ticketed, and closed by a re-test."
    ),
    "icon": "ListOrdered",
    "platform_targets": ["xtm_one", "dev"],
    "orchestrator": "ctem-vuln-prioritization-orchestrator",
    "members": [
        {"agent": "ctem-vuln-prioritization-orchestrator", "role_label": "Orchestrator"},
        {
            "agent": "ctem-vuln-triage-analyst",
            "role_label": "Triage",
            "parent": "ctem-vuln-prioritization-orchestrator",
        },
    ],
    "connected": [
        {
            "agent": "ctem-risk-quantifier",
            "collaboration_instruction": "Quantifies a validated finding in OpenCRQ (expected-loss delta)",
        },
        {
            "agent": "ctem-remediation-coordinator",
            "collaboration_instruction": "Drafts the remediation ticket and the detections for a validated finding",
        },
        {
            "agent": "openaev-payload-creator",
            "collaboration_instruction": "Consulted for the exploitability validation payload",
        },
        {
            "agent": "opencti-detection-rule-converter",
            "collaboration_instruction": "Consulted for detections of the exploited technique",
        },
        {"agent": "opencti-assistant", "collaboration_instruction": "Consulted for OpenCTI context on a CVE"},
    ],
    "entry_handovers": [{"from": "ctem-assistant", "type": "delegate"}],
}
BUILTIN_FLEETS.append(CTEM_VULN_PRIORITIZATION_FLEET)
#: F5, third-party risk (s.9.5, #9157 M5, decision D1 = A): enabled on its own.
BUILTIN_FLEETS.append(THIRD_PARTY_RISK_FLEET)
#: M1, OpenCTI PIR intelligence desk (s.13, #10542): the single-product fleet of OpenCTI.
BUILTIN_FLEETS.append(PIR_DESK_FLEET)
#: M2, OpenAEV detection validation desk (s.13.2, #10575): the single-product fleet of OpenAEV.
BUILTIN_FLEETS.append(DETECTION_DESK_FLEET)
#: M3, OpenCRQ risk budget desk (s.13.3, #10593): the single-product fleet of OpenCRQ.
BUILTIN_FLEETS.append(RISK_BUDGET_DESK_FLEET)
