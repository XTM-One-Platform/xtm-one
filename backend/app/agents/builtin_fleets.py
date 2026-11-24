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

CTEM-31 (#1048, Sam, 21 Nov 2026): exactly FOUR seeded fleets, one generic
CTEM loop and three use cases (``dev-docs/ctem-fleets.md`` section 14). Adding
a fifth is a product decision, never a fix (``tests/test_builtin_fleets.py``).
Two optional keys carry the upgrade of an instance seeded with the former eight:

* ``previous_slugs``: the built-in fleet row stored under one of them is adopted
  (renamed) instead of creating a new one, so its id, workspace, autonomy level,
  pins and the forks pointing at it carry over;
* ``absorbs``: the retired fleets folded into this one. Their row is swept, their
  agents become members of this fleet (their assignments, enablement and run-as
  never move: they live on the agents), their pins carry over, and a fork of one
  of them names this fleet as its successor (:data:`RETIRED_FLEET_SUCCESSORS`).
* ``leads``: the members whose enabled automation makes the fleet active (its
  orchestrator always counts): a folded fleet's former orchestrator stays a lead,
  so an automation switched on before the upgrade keeps the fleet active.
"""

from __future__ import annotations

from typing import Any

from app.agents.ctem_ai_security import AI_SECURITY_FLEET
from app.agents.ctem_third_party_risk import THIRD_PARTY_RISK_FLEET
from app.agents.openaev_detection_desk import DETECTION_DESK_FLEET
from app.agents.opencrq_risk_budget_desk import RISK_BUDGET_DESK_FLEET
from app.agents.opencti_pir_desk import PIR_DESK_FLEET

#: The generic fleet's orchestrator: the former F1 root, now the root of the whole loop.
_LOOP = "ctem-threat-response-orchestrator"
_POSTURE = "ctem-posture-orchestrator"

CTEM_LOOP_SLUG = "ctem-loop"


def _folded(fleet: dict[str, Any], role_label: str) -> list[dict[str, Any]]:
    """The members of a retired *fleet* re-homed under the loop orchestrator, its root labelled *role_label*."""
    root = fleet["orchestrator"]
    out: list[dict[str, Any]] = []
    for member in fleet["members"]:
        if member["agent"] == root:
            out.append({"agent": root, "role_label": role_label, "parent": _LOOP, "exposed": False})
        else:
            out.append({**member, "exposed": False})
    return out


def _connected(*fleets: dict[str, Any], home: set[str], extra: list[dict[str, str]]) -> list[dict[str, str]]:
    """The connected agents of *fleets* and *extra*, first instruction kept, home members left out."""
    seen: dict[str, dict[str, str]] = {}
    for entry in [*extra, *(c for f in fleets for c in f.get("connected") or [])]:
        if entry["agent"] not in home and entry["agent"] not in seen:
            seen[entry["agent"]] = dict(entry)
    return list(seen.values())


_POSTURE_MEMBERS: list[dict[str, Any]] = [
    {"agent": _POSTURE, "role_label": "Posture", "parent": _LOOP, "exposed": False},
    *(
        {"agent": slug, "role_label": role, "parent": _POSTURE, "exposed": False}
        for slug, role in (
            ("ctem-scoping-analyst", "Scoping"),
            ("ctem-discovery-analyst", "Discovery"),
            ("ctem-prioritization-analyst", "Prioritization"),
            ("ctem-validation-analyst", "Validation"),
            ("ctem-mobilization-analyst", "Mobilization"),
            ("exposure-briefing-analyst", "Report"),
        )
    ),
]

_LOOP_MEMBERS: list[dict[str, Any]] = [
    {"agent": _LOOP, "role_label": "Orchestrator", "exposed": True},
    {"agent": "ctem-threat-relevance-analyst", "role_label": "Relevance", "parent": _LOOP, "exposed": False},
    {"agent": "openaev-scenario-creator", "role_label": "Validation", "parent": _LOOP, "exposed": False},
    {"agent": "ctem-risk-quantifier", "role_label": "Risk", "parent": _LOOP, "exposed": False},
    {"agent": "ctem-remediation-coordinator", "role_label": "Mobilization", "parent": _LOOP, "exposed": False},
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
    *_POSTURE_MEMBERS,
    *_folded(PIR_DESK_FLEET, "Intelligence desk"),
    *_folded(DETECTION_DESK_FLEET, "Validation desk"),
    *_folded(RISK_BUDGET_DESK_FLEET, "Risk desk"),
]

#: 1. CTEM loop, the generic fleet (CTEM-31): every trigger path, end to end on
#: a case. The former F1 threat response is its core; F4 posture and the three
#: single-product desks are folded in as roles, so each module keeps its
#: specialist and an automation switched on before the upgrade keeps running.
CTEM_LOOP_FLEET: dict[str, Any] = {
    "slug": CTEM_LOOP_SLUG,
    "previous_slugs": ["ctem-threat-response"],
    "absorbs": ["ctem-posture", PIR_DESK_FLEET["slug"], DETECTION_DESK_FLEET["slug"], RISK_BUDGET_DESK_FLEET["slug"]],
    "name": "CTEM loop",
    "description": (
        "The whole CTEM loop on your software, case by case: any change in exposure (a CVE, a threat, "
        "a leak, a supplier event, a regression) is reconciled, gated on your scope, opened as a "
        "case, validated in OpenAEV, priced in OpenCRQ, mobilised with one decision of yours and "
        "closed by a passing re-test. It also keeps the posture, the PIR answers, the detection "
        "coverage and the risk register current."
    ),
    "icon": "Workflow",
    "orchestrator": _LOOP,
    "leads": [
        _POSTURE,
        PIR_DESK_FLEET["orchestrator"],
        DETECTION_DESK_FLEET["orchestrator"],
        RISK_BUDGET_DESK_FLEET["orchestrator"],
    ],
    "members": _LOOP_MEMBERS,
    # The posture lead answers people in chat (where the organization stands), as it
    # did as the orchestrator of the posture fleet; every other role is reached
    # through the loop orchestrator.
    "chat_members": [_POSTURE],
    "connected": _connected(
        PIR_DESK_FLEET,
        DETECTION_DESK_FLEET,
        RISK_BUDGET_DESK_FLEET,
        home={m["agent"] for m in _LOOP_MEMBERS},
        extra=[
            {
                "agent": "ctem-assistant",
                "collaboration_instruction": "Entry point; delegates 'are we at risk' questions",
            },
            {
                "agent": "deep-investigation-agent",
                "collaboration_instruction": "Consulted for open-source context on a threat",
            },
            {
                "agent": "opencti-dashboard-widget-creator",
                "collaboration_instruction": "Consulted to turn a posture finding into an OpenCTI dashboard widget",
            },
        ],
    ),
    "entry_handovers": [{"from": "ctem-assistant", "type": "delegate"}],
}

#: 2. Vulnerability management (F2, s.4.2, #9120): path 1, a vulnerability on
#: software we run, to a verified fix. The loop's role agents (risk quantifier,
#: remediation coordinator) are CONNECTED here, never members: an agent has
#: one home fleet, the generic one.
CTEM_VULN_PRIORITIZATION_FLEET: dict[str, Any] = {
    "slug": "ctem-vuln-prioritization",
    "name": "Vulnerability management",
    "description": (
        "Which CVEs do we fix on Monday: a vulnerability on software you run, KEV, EPSS jumps and "
        "sightings matched to your assets, validated in OpenAEV, priced in OpenCRQ, ticketed, and "
        "closed by a re-test."
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

#: The four seeded fleets, in the order the fleet list shows them: the generic
#: loop first, then the use cases (3. AI security, 4. third-party and supply chain).
BUILTIN_FLEETS: list[dict[str, Any]] = [
    CTEM_LOOP_FLEET,
    CTEM_VULN_PRIORITIZATION_FLEET,
    AI_SECURITY_FLEET,
    THIRD_PARTY_RISK_FLEET,
]

#: Retired built-in fleet slug -> the seeded fleet that took over its work.
RETIRED_FLEET_SUCCESSORS: dict[str, str] = {
    old: entry["slug"]
    for entry in BUILTIN_FLEETS
    for old in [*(entry.get("previous_slugs") or []), *(entry.get("absorbs") or [])]
}

#: Built-in fleet slug -> the agent slugs whose enabled automation makes it
#: active: its orchestrator and its declared ``leads``.
BUILTIN_FLEET_LEADS: dict[str, frozenset[str]] = {
    entry["slug"]: frozenset([entry["orchestrator"], *(entry.get("leads") or [])]) for entry in BUILTIN_FLEETS
}

#: Every lead agent slug of every built-in fleet (the SQL activity rule of
#: ``app.services.fleet_runs``).
BUILTIN_FLEET_LEAD_SLUGS: frozenset[str] = frozenset(s for leads in BUILTIN_FLEET_LEADS.values() for s in leads)
