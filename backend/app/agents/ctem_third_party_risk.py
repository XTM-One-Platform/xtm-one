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

"""CTEM seeded fleet F5: third-party risk (#9157, epic #8975, slice M5).

``dev-docs/ctem-fleets.md`` sections 9.5 and 9.6, decisions D1 = A and D3 = A.
One new built-in agent (the orchestrator), its assignments and the fleet entry;
:mod:`app.agents.builtin_agents` and :mod:`app.agents.builtin_fleets` extend
their lists with them.

- ``ctem-third-party-risk-orchestrator`` turns an OpenCTI event about a tracked
  organization (``provision.organizations`` ``tracked``, slice M4) into a
  supplier-exposure case: the threat, the organization and its relationship,
  the dependency, the expected loss read from OpenCRQ and the proposed actions.
  Every action is an approval work item for a person (``needs_human``); no tool
  of the fleet writes to the third party.
- The CTEM role agents F5 works with (relevance, risk, remediation) are seeded
  by F1 and home in its fleet; F5 connects them by slug (D1 = A). A slug this
  deployment does not seed is skipped by the fleet and handover seeds.

The fleet is admin opt-in and off by default like every seeded fleet
(``SEEDED_FLEET_OPT_IN``, #9160): nothing runs, provisions or meters quota
until an administrator switches an assignment on.
"""

from __future__ import annotations

from app.agents.fleet_opt_in import SEEDED_FLEET_OPT_IN

ORCHESTRATOR = "ctem-third-party-risk-orchestrator"
FLEET_SLUG = "ctem-third-party-risk"
FLEET_NAME = "Third-party risk"

#: Work-item key prefix of the approval items (section 4 common rules).
APPROVAL_PREFIX = "ctem:f5:approval:"

#: F1 agents F5 works with; connected, never members (they are home in F1).
SHARED_AGENTS: tuple[str, ...] = (
    "ctem-threat-relevance-analyst",
    "ctem-risk-quantifier",
    "ctem-remediation-coordinator",
)

#: The actions a supplier-exposure case may propose; each one is an approval item.
PROPOSED_ACTIONS: tuple[str, ...] = (
    "contact_supplier",
    "compensating_control",
    "contract_clause",
    "monitoring_raised",
)

_TARGETS = ["xtm_one", "dev"]

_CASE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "case_type": {"type": "string", "enum": ["supplier_exposure", "awareness"]},
        "threat": {"type": "string", "description": "The threat or breach (OpenCTI entity name and id)."},
        "organization": {"type": "string", "description": "The tracked organization concerned."},
        "relationship": {"type": "string", "enum": ["provider", "customer", "partner", "subsidiary"]},
        "dependency": {
            "type": "object",
            "properties": {
                "services": {"type": "array", "items": {"type": "string"}},
                "primary_assets": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["services", "primary_assets"],
        },
        "expected_loss": {
            "type": "object",
            "description": "Read from the primary's OpenCRQ scenarios reached through the supporting asset.",
            "properties": {
                "amount": {"type": "number"},
                "currency": {"type": "string"},
                "supporting_asset": {
                    "type": "string",
                    "description": "The organization name and the service name, e.g. Acme - Payroll.",
                },
                "scenarios": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["scenarios"],
        },
        "proposed_actions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": list(PROPOSED_ACTIONS)},
                    "detail": {"type": "string"},
                    "approval_item": {"type": "string", "description": "Key of the approval work item."},
                },
                "required": ["action", "detail", "approval_item"],
            },
        },
        "status": {
            "type": "string",
            "enum": ["not_relevant", "awaiting_approval", "awareness_only", "no_tracked_organization"],
        },
        "missing_context": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["case_type", "threat", "organization", "proposed_actions", "status", "missing_context"],
}

THIRD_PARTY_RISK_AGENTS: list[dict] = [
    {
        "slug": ORCHESTRATOR,
        "persona": ORCHESTRATOR,
        "name": "Third-Party Orchestrator",
        "description": (
            "Orchestrates the third-party risk fleet: turns a threat or breach at a tracked "
            "supplier, customer or partner into a case with its dependency, the expected loss "
            "read from OpenCRQ and actions proposed behind approval. Never contacts a third party."
        ),
        "icon": "Handshake",
        "output_format": "json",
        "output_schema": _CASE_SCHEMA,
        # A case orchestrator (JSON case schema), reached by delegation and its
        # assignments; nobody talks to it directly.
        "disable_chat": True,
        "max_iterations": 120,
        "temperature": 0.2,
        "tags": ["builtin", "ctem", "fleet", "third-party-risk"],
        "platform_targets": _TARGETS,
        "builtin_tool_ids": ["work_items", "shared_state"],
        "integration_template_bindings": ["opencti", "opencrq"],
        "handover_agent_slugs": list(SHARED_AGENTS),
        "handover_type_slugs": {s: "delegate" for s in SHARED_AGENTS},
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

THIRD_PARTY_RISK_ASSIGNMENTS: list[dict] = [
    {
        **_COMMON,
        "name": "Tracked organization named",
        "description": (
            "An OpenCTI report or incident names a tracked organization (or its sector): "
            "open the supplier-exposure or awareness case."
        ),
        "trigger_type": "integration_event",
        "trigger_config": {
            "provider": "opencti",
            "event": "create",
            "provision": {
                "fleet": FLEET_NAME,
                "purpose": "Tracked organizations",
                "entity_types": ["Report", "Incident"],
                "event_types": ["create"],
                "organizations": "tracked",
            },
        },
        "prompt": (
            "An OpenCTI {{trigger.entity_type}} was created: {{trigger.entity_name}} ({{trigger.entity_id}}). "
            "The run block names the tracked organizations it concerns.\n"
            "Each tracked organization this event names by identity already has a third_party_event "
            "signal (path 11, recorded before this run): the scope gate opens its CTEM case when a primary "
            "asset depends on it, else watches it. Read it with list_ctem_signals when you hold it.\n"
            "Run one case of the third-party risk loop: a provider gets a supplier-exposure case "
            "(dependency, expected loss from OpenCRQ, actions behind approval work items); a "
            "customer, partner or subsidiary gets an awareness case with no action toward them. "
            "Never contact a third party. Answer with the case JSON."
        ),
    },
    {
        **_COMMON,
        "name": "Weekly third-party review",
        "description": "Weekly pass over the open third-party cases and the tracked providers of criticality high or critical.",
        "trigger_type": "schedule",
        "trigger_config": {"cron": "0 7 * * 3"},
        "prompt": (
            "Weekly third-party review. Read the open cases of this fleet (shared state ctem:f5:state), "
            "the approval items still waiting, and the OpenCTI activity of the last 7 days on the "
            "tracked providers of criticality high or critical. Refresh the expected loss of each "
            "open supplier-exposure case from OpenCRQ and close what no longer applies. Answer with "
            "the case JSON of the most important case and list the others in its missing_context."
        ),
    },
]

THIRD_PARTY_RISK_FLEET: dict = {
    "slug": FLEET_SLUG,
    "name": FLEET_NAME,
    "description": (
        "Watches the organization's tracked suppliers, customers and partners in OpenCTI, "
        "prices a supplier exposure from the primary's OpenCRQ scenarios and proposes "
        "actions behind approval. Never contacts a third party."
    ),
    "icon": "Handshake",
    "platform_targets": _TARGETS,
    "orchestrator": ORCHESTRATOR,
    "members": [
        {"agent": ORCHESTRATOR, "role_label": "Orchestrator", "exposed": True},
    ],
    "connected": [
        {
            "agent": "ctem-threat-relevance-analyst",
            "collaboration_instruction": "Relevance of the event to the tracked organization",
        },
        {
            "agent": "ctem-risk-quantifier",
            "collaboration_instruction": "Reads the expected loss from the primary's OpenCRQ scenarios",
        },
        {
            "agent": "ctem-remediation-coordinator",
            "collaboration_instruction": "Drafts the compensating control after approval",
        },
        {"agent": "ctem-assistant", "collaboration_instruction": "Entry point; delegates third-party questions"},
        {"agent": "deep-investigation-agent", "collaboration_instruction": "Open-source context on a supplier breach"},
    ],
    "entry_handovers": [{"from": "ctem-assistant", "type": "delegate"}],
}
