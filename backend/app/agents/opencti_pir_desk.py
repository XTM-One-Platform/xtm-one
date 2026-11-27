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

"""Single-product seeded fleet M1: the OpenCTI PIR intelligence desk (#10542, epic #8975).

``dev-docs/ctem-fleets.md`` section 13. The CTEM fleets F1 to F5 need two or
three modules; this one works with OpenCTI alone, for a CTI team running a
priority intelligence requirement (PIR) programme. Every PIR of the
organization gets a ranked, current answer instead of a flow of reports.

Three new built-in agents, their assignments and the fleet entry;
:mod:`app.agents.builtin_agents` and :mod:`app.agents.builtin_fleets` extend
their lists with them.

- ``opencti-pir-desk-orchestrator`` runs the loop: triage, one standing case
  per PIR, hunt package and detections for the top threats, the weekly answer,
  the half-year landscape and the stale indicator clean-up.
- ``opencti-pir-triage-analyst`` scores a new entity against each PIR and the
  organization profile (``threat_report_triaged``).
- ``opencti-landscape-reporter`` writes the weekly answer per PIR and the
  half-year threat landscape (``briefing_written``).
- The existing OpenCTI agents are connected by slug, never moved, renamed or
  changed: their chat, intents and the 14 dashboard and decay automations
  behave as before.

The PIR read is today's (sectors and regions as criteria); a criteria type a
PIR carries is used when present and never required. The fleet is admin
opt-in and off by default like every seeded fleet (``SEEDED_FLEET_OPT_IN``,
#9160): nothing runs, provisions or meters quota until an administrator
switches an assignment on.
"""

from __future__ import annotations

from typing import Any

from app.agents.fleet_opt_in import SEEDED_FLEET_OPT_IN
from app.agents.gate_answered import gate_answered_assignment

ORCHESTRATOR = "opencti-pir-desk-orchestrator"
TRIAGE_ANALYST = "opencti-pir-triage-analyst"
LANDSCAPE_REPORTER = "opencti-landscape-reporter"
FLEET_SLUG = "opencti-pir-desk"
FLEET_NAME = "PIR intelligence desk"

#: Shared state of the fleet: the standing case of each PIR, last answers, counters.
STATE_KEY = "opencti:pir-desk:state"
#: Work-item key prefix of the approval items (indicator revocations).
APPROVAL_PREFIX = "opencti:pir-desk:approval:"

#: Existing OpenCTI agents the fleet works with; connected, never members.
SHARED_AGENTS: tuple[str, ...] = (
    "opencti-assistant",
    "opencti-entity-insights",
    "opencti-threat-hunt-creator",
    "opencti-detection-rule-converter",
    "opencti-dashboard-creator",
    "indicator-decay-guardian",
    "indicator-decay-judge",
)

#: The entity types whose creation the trigger listens to. Threat activity (Intrusion-Set,
#: Campaign) flagged in a PIR is the CTEM loop's one trigger, "Threat activity flagged in a PIR"
#: (#1832): one owner per entity type, so one entity is never triaged twice.
TRIGGER_ENTITY_TYPES: tuple[str, ...] = ("Report", "Vulnerability")

_TARGETS = ["xtm_one", "dev"]

_TRIAGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "entity": {"type": "string", "description": "The OpenCTI entity triaged (type, name and id)."},
        "matches": {
            "type": "array",
            "description": "One entry per PIR the entity answers; empty when it answers none.",
            "items": {
                "type": "object",
                "properties": {
                    "pir": {"type": "string", "description": "The PIR name and id."},
                    "score": {"type": "integer", "minimum": 0, "maximum": 100},
                    "criteria": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "The PIR criteria the entity meets (sector, region, or another type the PIR carries).",
                    },
                    "evidence": {"type": "string"},
                    "file_in_case": {"type": "boolean"},
                },
                "required": ["pir", "score", "criteria", "evidence", "file_in_case"],
            },
        },
        "top_threat": {"type": "boolean", "description": "Worth a hunt package and detections."},
        "missing_context": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["entity", "matches", "top_threat", "missing_context"],
}

_REPORT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "headline": {"type": "string", "description": "The answer in one sentence."},
        "period": {"type": "string", "description": "The period covered, e.g. the last 7 days or H2."},
        "kind": {"type": "string", "enum": ["weekly_pir_answer", "threat_landscape"]},
        "pir_answers": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "pir": {"type": "string"},
                    "answer": {"type": "string"},
                    "ranked_threats": {"type": "array", "items": {"type": "string"}},
                    "changes": {"type": "string", "description": "What changed since the last answer."},
                    "case": {"type": "string", "description": "The standing case of the PIR (OpenCTI id)."},
                },
                "required": ["pir", "answer", "ranked_threats"],
            },
        },
        "report_id": {"type": "string", "description": "The OpenCTI report written, if any."},
        "missing_context": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["headline", "kind", "pir_answers", "missing_context"],
}

_RUN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "triaged": {"type": "array", "items": {"type": "string"}},
        "cases_updated": {"type": "array", "items": {"type": "string"}},
        "hunt_packages": {"type": "array", "items": {"type": "string"}},
        "detections": {"type": "array", "items": {"type": "string"}},
        "approval_items": {"type": "array", "items": {"type": "string"}},
        "missing_context": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "missing_context"],
}

_NO_MEMORY = {
    "memory_short_term_enabled": False,
    "memory_medium_term_enabled": False,
    "memory_persistent_enabled": False,
    "memory_user_enabled": False,
}

_ROLE_BASE: dict[str, Any] = {
    "output_format": "json",
    "disable_chat": True,
    "temperature": 0.2,
    "platform_targets": _TARGETS,
    "handover_agent_slugs": [],
    "handover_type_slugs": {},
    **_NO_MEMORY,
}

_DELEGATES = (TRIAGE_ANALYST, LANDSCAPE_REPORTER, "opencti-threat-hunt-creator", "indicator-decay-guardian")
_CONSULTS = ("opencti-entity-insights", "opencti-detection-rule-converter", "opencti-dashboard-creator")

PIR_DESK_AGENTS: list[dict[str, Any]] = [
    {
        "slug": ORCHESTRATOR,
        "persona": ORCHESTRATOR,
        "name": "PIR Desk Orchestrator",
        "description": (
            "Orchestrates the PIR intelligence desk: triages what OpenCTI receives against the "
            "organization's PIRs, keeps one standing case per PIR, builds hunt packages and "
            "detections for the top threats, and answers each PIR every week."
        ),
        "icon": "Radar",
        "output_format": "json",
        "output_schema": _RUN_SCHEMA,
        # A run orchestrator (JSON run schema): driven by its assignments, never a chat.
        "disable_chat": True,
        "max_iterations": 150,
        "temperature": 0.2,
        "tags": ["builtin", "opencti", "cti", "fleet", "pir-desk"],
        "platform_targets": _TARGETS,
        "builtin_tool_ids": ["work_items", "shared_state", "generate_file"],
        "integration_template_bindings": ["opencti"],
        "handover_agent_slugs": [*_DELEGATES, *_CONSULTS],
        "handover_type_slugs": {**{s: "delegate" for s in _DELEGATES}, **{s: "consult" for s in _CONSULTS}},
        **_NO_MEMORY,
    },
    {
        **_ROLE_BASE,
        "slug": TRIAGE_ANALYST,
        "persona": TRIAGE_ANALYST,
        "name": "PIR Triage Analyst",
        "description": (
            "Scores a new OpenCTI report, intrusion set, campaign or vulnerability against each PIR "
            "and the organization profile, with the evidence of each criterion met."
        ),
        "icon": "Target",
        "output_schema": _TRIAGE_SCHEMA,
        "tags": ["builtin", "opencti", "cti", "fleet"],
        "builtin_tool_ids": [],
        "integration_template_bindings": ["opencti"],
        # It scores entities and only reads OpenCTI: no write tool is reachable.
        "integration_template_read_only": ["opencti"],
    },
    {
        **_ROLE_BASE,
        "slug": LANDSCAPE_REPORTER,
        "persona": LANDSCAPE_REPORTER,
        "name": "Landscape Reporter",
        "description": (
            "Writes the weekly answer of each PIR (ranked threats, what changed) and the half-year "
            "threat landscape report from the PIR cases in OpenCTI."
        ),
        "icon": "Newspaper",
        "output_schema": _REPORT_SCHEMA,
        "tags": ["builtin", "opencti", "cti", "fleet"],
        "builtin_tool_ids": ["generate_file"],
        "integration_template_bindings": ["opencti"],
    },
]

_COMMON = {
    "agent_slug": ORCHESTRATOR,
    **SEEDED_FLEET_OPT_IN,
    "ai_label": False,
    "notify_on_complete": False,
    "show_in_digest": True,
    "max_iterations": 150,
    "timeout_seconds": 1800,
}

PIR_DESK_ASSIGNMENTS: list[dict[str, Any]] = [
    {
        **_COMMON,
        "name": "PIR match created",
        "description": (
            "An OpenCTI report or vulnerability is linked to an entity flagged in one of the "
            "organization's PIRs: triage it and file what matters in the PIR case. Threat activity "
            "flagged in a PIR is the CTEM loop's trigger."
        ),
        "trigger_type": "integration_event",
        "trigger_config": {
            "provider": "opencti",
            "event": "create",
            "provision": {
                "fleet": FLEET_NAME,
                "purpose": "PIR matches",
                # A report or a vulnerability is never flagged in a PIR itself (#2583 point 3): the
                # PIR playbook delivers it when a new relationship links it to a flagged entity.
                "mode": "pir_playbook",
                "pir_relation": "linked",
                "entity_types": list(TRIGGER_ENTITY_TYPES),
                "organization_context": ["pir_ids"],
            },
        },
        "prompt": (
            "An OpenCTI {{trigger.entity_type}} was created: {{trigger.entity_name}} ({{trigger.entity_id}}). "
            "A new relationship links it to an entity flagged in at least one of the organization's PIRs.\n"
            "Triage each STIX id once: when the standing case of its PIR already holds this entity, "
            "add nothing and answer that it was filed before.\n"
            "Run one triage of the PIR desk loop: delegate the scoring to the PIR Triage Analyst, "
            "file each match worth it in the standing case of its PIR, and when it is a top threat "
            "have the hunt package and the detections built. Answer with the run JSON."
        ),
    },
    {
        **_COMMON,
        "name": "Weekly PIR answer",
        "description": "Every Monday, the ranked and current answer of each PIR.",
        "trigger_type": "schedule",
        "trigger_config": {"cron": "0 7 * * 1"},
        "prompt": (
            "Weekly PIR answer. Read the standing case of each PIR (shared state "
            f"{STATE_KEY}) and what OpenCTI received in the last 7 days. Delegate the weekly answer "
            "of each PIR to the Landscape Reporter: the ranked threats, what changed, what to do. "
            "Answer with the run JSON."
        ),
    },
    {
        **_COMMON,
        "name": "Half-year threat landscape",
        "description": "On 1 January and 1 July, the threat landscape of the last six months across every PIR.",
        "trigger_type": "schedule",
        "trigger_config": {"cron": "0 8 1 1,7 *"},
        "prompt": (
            "Half-year threat landscape. Delegate to the Landscape Reporter the report of the last six "
            "months across every PIR: the threats ranked, the trends, the gaps in coverage. Have it "
            "written as an OpenCTI report. Answer with the run JSON."
        ),
    },
    {
        **_COMMON,
        "name": "Stale indicator clean-up",
        "description": "Monthly pass on the indicators of the PIR cases that went stale.",
        "trigger_type": "schedule",
        "trigger_config": {"cron": "0 6 1 * *"},
        "prompt": (
            "Stale indicator clean-up. List the indicators of the PIR cases with no sighting and no "
            "new evidence for 90 days. Have the Indicator Decay Guardian re-check them; each one still "
            "to revoke becomes an approval work item for a person, keyed "
            f"{APPROVAL_PREFIX} followed by the indicator's OpenCTI id. Never revoke one yourself. "
            "Answer with the run JSON."
        ),
    },
    gate_answered_assignment(
        _COMMON,
        prefixes=(APPROVAL_PREFIX,),
        inherit_switch_from="PIR match created",
        description="A person answered an indicator revocation: revoke it, or record the refusal.",
        apply=(
            "Approved: revoke exactly the indicator the item names in OpenCTI and record it on its "
            "PIR case. Rejected or deferred: keep the indicator and record the decision and the "
            "reason on the case. Answer with the run JSON."
        ),
    ),
]

PIR_DESK_FLEET: dict[str, Any] = {
    "slug": FLEET_SLUG,
    "name": FLEET_NAME,
    "description": (
        "For CTI teams running OpenCTI alone: every PIR of the organization gets a ranked, current "
        "answer, one standing case per PIR, hunt packages and detections for the top threats."
    ),
    "icon": "Radar",
    "platform_targets": _TARGETS,
    "orchestrator": ORCHESTRATOR,
    "members": [
        {"agent": ORCHESTRATOR, "role_label": "Orchestrator", "exposed": True},
        {"agent": TRIAGE_ANALYST, "role_label": "Triage", "parent": ORCHESTRATOR, "exposed": False},
        {"agent": LANDSCAPE_REPORTER, "role_label": "Reporting", "parent": ORCHESTRATOR, "exposed": False},
    ],
    "connected": [
        {"agent": "opencti-assistant", "collaboration_instruction": "Answers the CTI team's questions in chat"},
        {"agent": "opencti-entity-insights", "collaboration_instruction": "Context on a triaged entity"},
        {"agent": "opencti-threat-hunt-creator", "collaboration_instruction": "Hunt package for a top threat"},
        {
            "agent": "opencti-detection-rule-converter",
            "collaboration_instruction": "Detections from the hunt package indicators",
        },
        {"agent": "opencti-dashboard-creator", "collaboration_instruction": "A PIR dashboard on request"},
        {"agent": "indicator-decay-guardian", "collaboration_instruction": "Re-checks stale indicators"},
        {"agent": "indicator-decay-judge", "collaboration_instruction": "Rules on a stale indicator"},
    ],
}
