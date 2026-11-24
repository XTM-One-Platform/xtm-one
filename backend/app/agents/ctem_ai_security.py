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

"""CTEM seeded fleet F3: AI security (#9113, epic #8975).

``dev-docs/ctem-fleets.md`` section 4.3. Two new built-in agents, their
assignments and the fleet entry; :mod:`app.agents.builtin_agents` and
:mod:`app.agents.builtin_fleets` extend their lists with them.

- ``ctem-ai-security-orchestrator`` runs the loop: an AI-enabled threat is
  mapped to MITRE ATLAS, matched to the AI targets of the organization profile,
  an OpenAEV AI test is proposed behind an approval work item, the result is
  priced and a fix is tracked to its re-test.
- ``ctem-ai-threat-analyst`` maps a report or a campaign to ATLAS techniques and
  proposes the ATLAS entities in an OpenCTI draft, never in the knowledge.

The agents F3 shares with F1 (relevance, risk, remediation, scenario creator)
are seeded by F1 and home in its fleet; F3 connects them by slug. A slug this
deployment does not seed is skipped by the fleet and handover seeds, so F3
works alone and gains them when they land.
"""

from __future__ import annotations

from app.agents.fleet_opt_in import SEEDED_FLEET_OPT_IN

ORCHESTRATOR = "ctem-ai-security-orchestrator"
THREAT_ANALYST = "ctem-ai-threat-analyst"
FLEET_SLUG = "ctem-ai-security"
FLEET_NAME = "AI security"

#: Work-item key prefixes of the fleet (section 4 common rules).
AI_SYSTEM_PREFIX = "ctem:f3:ai-system:"
APPROVAL_PREFIX = "ctem:f3:approval:"

#: F1 agents F3 works with; connected, never members (they are home in F1).
SHARED_AGENTS: tuple[str, ...] = (
    "ctem-threat-relevance-analyst",
    "openaev-scenario-creator",
    "ctem-risk-quantifier",
    "ctem-remediation-coordinator",
)

_TARGETS = ["xtm_one", "dev"]

_MAPPING_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "source": {"type": "string", "description": "The report, campaign or entity mapped (name and OpenCTI id)."},
        "summary": {"type": "string"},
        "atlas_techniques": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "technique_id": {"type": "string", "description": "ATLAS id, e.g. AML.T0051"},
                    "name": {"type": "string"},
                    "evidence": {"type": "string", "description": "The passage of the source that supports it."},
                    "confidence": {"type": "integer", "minimum": 0, "maximum": 100},
                },
                "required": ["technique_id", "name", "evidence", "confidence"],
            },
        },
        "ai_tradecraft": {
            "type": "array",
            "items": {"type": "string"},
            "description": "AI-enabled tradecraft seen (deepfake, fake persona, prompt manipulation...).",
        },
        "draft_id": {"type": "string", "description": "OpenCTI draft holding the proposed entities, empty when none."},
        "missing_context": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["source", "summary", "atlas_techniques", "draft_id", "missing_context"],
}

_CASE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "threat": {"type": "string"},
        "atlas_techniques": {"type": "array", "items": {"type": "string"}},
        "matched_ai_targets": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "exposure": {"type": "string"},
                    "why": {"type": "string"},
                },
                "required": ["name", "why"],
            },
        },
        "proposed_tests": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "ai_system": {"type": "string"},
                    "test": {
                        "type": "string",
                        "enum": [
                            "prompt_injection",
                            "jailbreak",
                            "excessive_agency",
                            "tool_misuse",
                            "mcp_tool_poisoning",
                        ],
                    },
                    "approval_item": {"type": "string", "description": "Key of the approval work item."},
                },
                "required": ["ai_system", "test", "approval_item"],
            },
        },
        "exposure": {"type": "string", "description": "Test results read, or 'pending approval'."},
        "fixes": {"type": "array", "items": {"type": "string"}},
        "status": {"type": "string", "enum": ["not_relevant", "awaiting_approval", "tested", "fixed", "no_ai_system"]},
        "missing_context": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["threat", "matched_ai_targets", "proposed_tests", "status", "missing_context"],
}

AI_SECURITY_AGENTS: list[dict] = [
    {
        "slug": ORCHESTRATOR,
        "persona": ORCHESTRATOR,
        "name": "AI Security Orchestrator",
        "description": (
            "Orchestrates the AI security fleet: maps AI-enabled threats to MITRE ATLAS, "
            "matches them to the organization's AI targets, proposes OpenAEV AI tests "
            "behind approval and tracks exposure and fixes."
        ),
        "icon": "BrainCircuit",
        "output_format": "json",
        "output_schema": _CASE_SCHEMA,
        # A case orchestrator (JSON case schema), reached by delegation and its
        # assignments; nobody talks to it directly.
        "disable_chat": True,
        "max_iterations": 120,
        "temperature": 0.2,
        "tags": ["builtin", "ctem", "fleet", "ai-security"],
        "platform_targets": _TARGETS,
        "builtin_tool_ids": ["work_items", "shared_state"],
        "integration_template_bindings": ["opencti", "openaev"],
        "handover_agent_slugs": [THREAT_ANALYST, *SHARED_AGENTS],
        "handover_type_slugs": {THREAT_ANALYST: "delegate", **{s: "delegate" for s in SHARED_AGENTS}},
        "memory_short_term_enabled": False,
        "memory_medium_term_enabled": False,
        "memory_persistent_enabled": False,
        "memory_user_enabled": False,
    },
    {
        "slug": THREAT_ANALYST,
        "persona": THREAT_ANALYST,
        "name": "AI Threat Analyst",
        "description": (
            "Maps AI-enabled tradecraft and attacks on AI targets to MITRE ATLAS techniques "
            "and proposes the ATLAS entities in an OpenCTI draft for analyst review."
        ),
        "icon": "ScanEye",
        "output_format": "json",
        "output_schema": _MAPPING_SCHEMA,
        "disable_chat": True,
        "max_iterations": 100,
        "temperature": 0.1,
        "tags": ["builtin", "ctem", "fleet", "ai-security", "atlas"],
        "platform_targets": _TARGETS,
        "builtin_tool_ids": [],
        "integration_template_bindings": ["opencti", "mitre_atlas"],
        "handover_agent_slugs": ["cti-ttp-harvester-filigran-ai"],
        "handover_type_slugs": {"cti-ttp-harvester-filigran-ai": "consult"},
        "memory_short_term_enabled": False,
        "memory_medium_term_enabled": False,
        "memory_persistent_enabled": False,
        "memory_user_enabled": False,
    },
]

_COMMON = {
    "agent_slug": ORCHESTRATOR,
    **SEEDED_FLEET_OPT_IN,
    "ai_label": False,
    "notify_on_complete": False,
    "show_in_digest": True,
    "max_iterations": 120,
    "timeout_seconds": 1800,
}

AI_SECURITY_ASSIGNMENTS: list[dict] = [
    {
        **_COMMON,
        "name": "New AI-enabled report",
        "description": "An OpenCTI report labelled 'ai' was created: map it to ATLAS, then judge its relevance to the AI targets.",
        "trigger_type": "integration_event",
        "trigger_config": {
            "provider": "opencti",
            "event": "create",
            "provision": {
                "fleet": FLEET_NAME,
                "purpose": "AI-enabled reports",
                "entity_types": ["Report"],
                "labels": ["ai"],
                "event_types": ["create"],
            },
        },
        "prompt": (
            "A report was created in OpenCTI: {{trigger.entity_name}} ({{trigger.entity_id}}).\n"
            "Run one case of the AI security loop on it: delegate the ATLAS mapping to the AI "
            "Threat Analyst, match the techniques to the AI targets of the organization profile, "
            "propose the OpenAEV AI tests behind approval work items, and answer with the case "
            "JSON. A report with no AI-enabled tradecraft and no attack on an AI system closes "
            "as not_relevant with the reason."
        ),
    },
    {
        **_COMMON,
        "name": "Weekly AI threat watch",
        "description": "Weekly summary of the new AI campaigns with their ATLAS techniques, and the AI targets they reach.",
        "trigger_type": "schedule",
        "trigger_config": {"cron": "0 7 * * 2"},
        "prompt": (
            "Weekly AI threat watch. Read the OpenCTI reports, campaigns and intrusion sets of the "
            "last 7 days that carry the 'ai' label or an ATLAS technique, and the cases this fleet "
            "already holds (shared state ctem:f3:state). For each new one, run the loop once; for "
            "an AI system of the profile with no baseline test yet, propose one behind approval. "
            "Answer with the case JSON of the most important case and list the others in its fixes."
        ),
    },
    {
        **_COMMON,
        "name": "AI system registered",
        "description": "A work item whose key starts with ctem:f3:ai-system: (then the system name) asks for the baseline test proposal of a new AI system.",
        "trigger_type": "work_item",
        "trigger_config": {"correlation_prefix": AI_SYSTEM_PREFIX},
        "prompt": (
            "An AI system was registered in the organization profile (the work item names it). "
            "Read it with get_organization_context, map the ATLAS techniques its kind and exposure "
            "make reachable, and propose its baseline OpenAEV tests (prompt injection, jailbreak, "
            "excessive agency, tool misuse, MCP tool poisoning where it uses tools) behind approval "
            "work items. Answer with the case JSON."
        ),
    },
]

AI_SECURITY_FLEET: dict = {
    "slug": FLEET_SLUG,
    "name": FLEET_NAME,
    "description": (
        "Tracks AI-enabled tradecraft and attacks on AI as intelligence mapped to MITRE ATLAS, "
        "tests the organization's AI assistants and agents in OpenAEV behind approval, and "
        "reports exposure, fixes and AI risk."
    ),
    "icon": "BrainCircuit",
    "platform_targets": _TARGETS,
    "orchestrator": ORCHESTRATOR,
    "members": [
        {"agent": ORCHESTRATOR, "role_label": "Orchestrator", "exposed": True},
        {"agent": THREAT_ANALYST, "role_label": "ATLAS mapping", "parent": ORCHESTRATOR},
    ],
    "connected": [
        {"agent": "ctem-threat-relevance-analyst", "collaboration_instruction": "Relevance to the organization's AI targets"},
        {"agent": "openaev-scenario-creator", "collaboration_instruction": "Builds the AI tests, launched only after approval"},
        {"agent": "ctem-risk-quantifier", "collaboration_instruction": "AI risk scenario in OpenCRQ"},
        {"agent": "ctem-remediation-coordinator", "collaboration_instruction": "Fixes ticketed after approval, then re-tested"},
        {"agent": "cti-ttp-harvester-filigran-ai", "collaboration_instruction": "TTP extraction consulted by the ATLAS mapping"},
        {"agent": "ctem-assistant", "collaboration_instruction": "Entry point; delegates AI security questions"},
        {"agent": "deep-investigation-agent", "collaboration_instruction": "Open-source context on an AI campaign"},
    ],
    "entry_handovers": [{"from": "ctem-assistant", "type": "delegate"}],
}
