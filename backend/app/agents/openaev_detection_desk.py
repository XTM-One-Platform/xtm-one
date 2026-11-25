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

"""Single-product seeded fleet M2: the OpenAEV detection validation desk (#10575, epic #8975).

``dev-docs/ctem-fleets.md`` section 13.2. The CTEM fleets F1 to F5 need two or
three modules; this one works with OpenAEV alone, for a SOC or detection
engineering team that has to prove its EDR and SIEM work. Prevention,
detection and logging coverage is measured from simulations, and every gap
is closed and proven closed by a re-test.

Three new built-in agents, their assignments and the fleet entry;
:mod:`app.agents.builtin_agents` and :mod:`app.agents.builtin_fleets` extend
their lists with them.

- ``openaev-detection-desk-orchestrator`` runs the loop: plan simulations from
  the threat arsenal against the organization's assets, read the results when
  a simulation ends, have each gap sorted and its root cause found, draft the
  detection rule through the existing rule chain, raise the remediation work
  item and re-test the gap.
- ``openaev-coverage-gap-analyst`` sorts the results of one simulation
  (blocked, detected, logged, missed) and names the root cause of each gap:
  no rule, no log, or a broken collector (``simulation_result_analysed``).
- ``openaev-coverage-reporter`` writes the coverage report: prevention,
  detection and logging rates, the gaps still open and the gaps proven closed
  by a re-test (``retest_cycle``).
- The existing OpenAEV and remediation agents are connected by slug, never
  moved, renamed or changed: their chat, intents and the 14 dashboard and
  decay automations behave as before.

The findings data model and the logging validation are read when the
connected OpenAEV exposes them (feature-detected by the personas), never
required. The fleet is admin opt-in and off by default like every seeded
fleet (``SEEDED_FLEET_OPT_IN``, #9160): nothing runs, provisions or meters
quota until an administrator switches an assignment on. Launching a
simulation is its only gated step.
"""

from __future__ import annotations

from typing import Any

from app.agents.fleet_opt_in import SEEDED_FLEET_OPT_IN
from app.agents.gate_answered import gate_answered_assignment

ORCHESTRATOR = "openaev-detection-desk-orchestrator"
GAP_ANALYST = "openaev-coverage-gap-analyst"
COVERAGE_REPORTER = "openaev-coverage-reporter"
FLEET_SLUG = "openaev-detection-desk"
FLEET_NAME = "Detection validation desk"

#: Shared state of the fleet: the open gaps, their root cause, rule and re-test.
STATE_KEY = "openaev:detection-desk:state"
#: Work-item key prefix of the re-tests the fleet schedules for itself.
RETEST_PREFIX = "openaev:detection-desk:retest:"
#: Work-item key prefix of the remediation items (one per gap).
REMEDIATION_PREFIX = "openaev:detection-desk:gap:"
#: Work-item key prefix of the launch approvals the fleet files for a person (#1388).
LAUNCH_PREFIX = "openaev:detection-desk:launch:"

#: Existing agents the fleet works with; connected, never members.
SHARED_AGENTS: tuple[str, ...] = (
    "openaev-assistant",
    "openaev-scenario-creator",
    "openaev-scenario-publisher",
    "openaev-payload-creator",
    "remediation-rule-type-detector",
    "remediation-splunk-rule-generator",
    "remediation-crowdstrike-rule-generator",
    "aev-detection-rules-generator",
)

#: The verdicts of one expectation, from the strongest control to none.
VERDICTS: tuple[str, ...] = ("blocked", "detected", "logged", "missed")
#: The root causes the gap analyst can name.
ROOT_CAUSES: tuple[str, ...] = ("none", "no_rule", "no_log", "broken_collector", "matching_defect", "unknown")

_TARGETS = ["xtm_one", "dev"]

_GAP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "simulation": {"type": "string", "description": "The OpenAEV simulation analysed (name and id)."},
        "gaps": {
            "type": "array",
            "description": "One entry per inject and asset whose strongest control is not 'blocked'.",
            "items": {
                "type": "object",
                "properties": {
                    "technique": {"type": "string", "description": "The ATT&CK technique of the inject."},
                    "asset": {"type": "string", "description": "The asset or asset group targeted."},
                    "verdict": {"type": "string", "enum": list(VERDICTS)},
                    "missing": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["prevention", "detection", "logging"]},
                        "description": "The controls that failed.",
                    },
                    "root_cause": {"type": "string", "enum": list(ROOT_CAUSES)},
                    "evidence": {"type": "string", "description": "What in OpenAEV supports the verdict."},
                    "rule_target": {
                        "type": "string",
                        "description": "The security platform a detection rule is owed for, when one is.",
                    },
                },
                "required": ["technique", "asset", "verdict", "missing", "root_cause", "evidence"],
            },
        },
        "coverage": {
            "type": "object",
            "description": "Share of expectations met in this simulation, 0 to 100.",
            "properties": {
                "prevention": {"type": "number"},
                "detection": {"type": "number"},
                "logging": {"type": "number"},
            },
        },
        "silent_collectors": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Collectors with no result for this simulation or none for weeks.",
        },
        "missing_context": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["simulation", "gaps", "missing_context"],
}

_REPORT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "headline": {"type": "string", "description": "The coverage in one sentence."},
        "period": {"type": "string", "description": "The period covered, e.g. the last 7 days."},
        "coverage": {
            "type": "object",
            "properties": {
                "prevention": {"type": "number"},
                "detection": {"type": "number"},
                "logging": {"type": "number"},
            },
            "required": ["prevention", "detection", "logging"],
        },
        "closed_gaps": {
            "type": "array",
            "description": "Gaps proven closed by a re-test since the last report; each counts once.",
            "items": {"type": "string"},
        },
        "open_gaps": {"type": "array", "items": {"type": "string"}},
        "silent_collectors": {"type": "array", "items": {"type": "string"}},
        "missing_context": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["headline", "coverage", "closed_gaps", "open_gaps", "missing_context"],
}

_RUN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "simulations_planned": {"type": "array", "items": {"type": "string"}},
        "simulations_analysed": {"type": "array", "items": {"type": "string"}},
        "gaps_open": {"type": "array", "items": {"type": "string"}},
        "rules_drafted": {"type": "array", "items": {"type": "string"}},
        "remediation_items": {"type": "array", "items": {"type": "string"}},
        "retests": {"type": "array", "items": {"type": "string"}},
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

_DELEGATES = (
    GAP_ANALYST,
    COVERAGE_REPORTER,
    "openaev-scenario-creator",
    "openaev-scenario-publisher",
    "openaev-payload-creator",
)
_CONSULTS = (
    "remediation-rule-type-detector",
    "remediation-splunk-rule-generator",
    "remediation-crowdstrike-rule-generator",
    "aev-detection-rules-generator",
)

DETECTION_DESK_AGENTS: list[dict[str, Any]] = [
    {
        "slug": ORCHESTRATOR,
        "persona": ORCHESTRATOR,
        "name": "Detection Orchestrator",
        "description": (
            "Orchestrates the detection validation desk: plans OpenAEV simulations against the "
            "organization's assets, sorts every prevention, detection and logging gap, drafts the "
            "missing detection rules, raises the remediation and re-tests until the gap is closed."
        ),
        "icon": "ShieldCheck",
        "output_format": "json",
        "output_schema": _RUN_SCHEMA,
        # A run orchestrator (JSON run schema): driven by its assignments, never a chat.
        "disable_chat": True,
        "max_iterations": 150,
        "temperature": 0.2,
        "tags": ["builtin", "openaev", "detection", "fleet", "detection-desk"],
        "platform_targets": _TARGETS,
        "builtin_tool_ids": ["work_items", "shared_state", "generate_file"],
        "integration_template_bindings": ["openaev"],
        "handover_agent_slugs": [*_DELEGATES, *_CONSULTS],
        "handover_type_slugs": {**{s: "delegate" for s in _DELEGATES}, **{s: "consult" for s in _CONSULTS}},
        **_NO_MEMORY,
    },
    {
        **_ROLE_BASE,
        "slug": GAP_ANALYST,
        "persona": GAP_ANALYST,
        "name": "Coverage Gap Analyst",
        "description": (
            "Sorts the results of one OpenAEV simulation into blocked, detected, logged and missed, "
            "and names the root cause of each gap: no rule, no log or a broken collector."
        ),
        "icon": "ScanSearch",
        "output_schema": _GAP_SCHEMA,
        "tags": ["builtin", "openaev", "detection", "fleet"],
        "builtin_tool_ids": [],
        "integration_template_bindings": ["openaev"],
    },
    {
        **_ROLE_BASE,
        "slug": COVERAGE_REPORTER,
        "persona": COVERAGE_REPORTER,
        "name": "Coverage Reporter",
        "description": (
            "Writes the coverage report of the desk: prevention, detection and logging rates, the "
            "gaps still open and the gaps proven closed by a re-test."
        ),
        "icon": "Gauge",
        "output_schema": _REPORT_SCHEMA,
        "tags": ["builtin", "openaev", "detection", "fleet"],
        # shared_state: the open gaps it reports are read from openaev:detection-desk:state (#1631).
        "builtin_tool_ids": ["shared_state", "generate_file"],
        "integration_template_bindings": ["openaev"],
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

DETECTION_DESK_ASSIGNMENTS: list[dict[str, Any]] = [
    {
        **_COMMON,
        "name": "Simulation finished",
        "description": (
            "An OpenAEV simulation ended: sort its results, find the root cause of each gap, draft "
            "the missing detection rules and raise the remediation."
        ),
        "trigger_type": "integration_event",
        "trigger_config": {
            "provider": "openaev",
            "event": "simulation_finished",
            "provision": {
                "fleet": FLEET_NAME,
                "purpose": "Finished simulations",
                "events": ["simulation_finished"],
            },
        },
        "prompt": (
            "An OpenAEV simulation finished; the trigger information below names it.\n"
            "Run one analysis of the detection desk loop: delegate the sorting of its results to the "
            "Coverage Gap Analyst, draft a detection rule for each gap whose root cause is a missing "
            f"rule, raise one remediation work item per gap (keyed {REMEDIATION_PREFIX}, owned by you, "
            "needs_human with an ask card: a person makes the fix). When the "
            f"simulation is itself a re-test (a {RETEST_PREFIX} item names it), close each gap it "
            "proves closed. Answer with the run JSON."
        ),
    },
    {
        **_COMMON,
        "name": "Weekly validation plan",
        "description": (
            "Every Monday, the simulations to run this week from the threat arsenal against the "
            "organization's assets, and the health of the collectors."
        ),
        "trigger_type": "schedule",
        "trigger_config": {"cron": "0 6 * * 1"},
        "prompt": (
            "Weekly validation plan. Read the organization context, the open gaps in shared state "
            f"{STATE_KEY} and the OpenAEV threat arsenal. Have the scenarios that cover the techniques "
            "with no recent result built or reused against the primary assets, and propose each "
            f"launch as an approval keyed {LAUNCH_PREFIX} followed by the scenario id: a launch waits "
            "for a person. Check every collector for results in "
            "the last 14 days and name the silent ones. Answer with the run JSON."
        ),
    },
    {
        **_COMMON,
        "name": "Weekly coverage report",
        "description": "Every Friday, prevention, detection and logging coverage with the gaps open and closed.",
        "trigger_type": "schedule",
        "trigger_config": {"cron": "0 8 * * 5"},
        "prompt": (
            "Weekly coverage report. Delegate to the Coverage Reporter the report of the last 7 days: "
            "the prevention, detection and logging rates, the gaps still open with their root cause, "
            "and the gaps proven closed by a re-test. Answer with the run JSON."
        ),
    },
    {
        **_COMMON,
        "name": "Re-test due",
        "description": "A remediation is done: re-run the simulation that found the gap and close it on proof.",
        "trigger_type": "work_item",
        "trigger_config": {"correlation_prefix": RETEST_PREFIX},
        "prompt": (
            "A re-test is due for the gap in the work item below. Propose the re-run of the "
            "simulation (or the atomic test) that found it as an approval keyed "
            f"{LAUNCH_PREFIX} followed by the scenario id: a launch waits for a person. Record the "
            f"re-test in shared state {STATE_KEY}; the gap closes only when the "
            "re-run result shows the control. Answer with the run JSON."
        ),
    },
    gate_answered_assignment(
        _COMMON,
        prefixes=(LAUNCH_PREFIX, REMEDIATION_PREFIX),
        inherit_switch_from="Simulation finished",
        description=(
            "A person answered a launch approval or a remediation: launch the simulation or schedule "
            "the re-test, or record the refusal."
        ),
        apply=(
            f"A launch ({LAUNCH_PREFIX}) approved: launch exactly the simulation the approval named, and "
            f"record it in shared state {STATE_KEY}; its results come back through Simulation finished. "
            f"A remediation ({REMEDIATION_PREFIX}) answered done: create the re-test work item keyed "
            f"{RETEST_PREFIX} followed by the same ids, owned by you (none when one is already open). "
            f"Rejected, declined or deferred: record the decision and the reason in {STATE_KEY}. "
            "Answer with the run JSON."
        ),
    ),
]

DETECTION_DESK_FLEET: dict[str, Any] = {
    "slug": FLEET_SLUG,
    "name": FLEET_NAME,
    "description": (
        "For SOC and detection engineering teams running OpenAEV alone: measured prevention, "
        "detection and logging coverage, every gap closed and proven closed by a re-test."
    ),
    "icon": "ShieldCheck",
    "platform_targets": _TARGETS,
    "orchestrator": ORCHESTRATOR,
    "members": [
        {"agent": ORCHESTRATOR, "role_label": "Orchestrator", "exposed": True},
        {"agent": GAP_ANALYST, "role_label": "Gap analysis", "parent": ORCHESTRATOR, "exposed": False},
        {"agent": COVERAGE_REPORTER, "role_label": "Reporting", "parent": ORCHESTRATOR, "exposed": False},
    ],
    "connected": [
        {"agent": "openaev-assistant", "collaboration_instruction": "Answers the team's questions in chat"},
        {"agent": "openaev-scenario-creator", "collaboration_instruction": "Builds a scenario for a technique"},
        {"agent": "openaev-scenario-publisher", "collaboration_instruction": "Publishes the planned scenario"},
        {"agent": "openaev-payload-creator", "collaboration_instruction": "A payload a scenario lacks"},
        {"agent": "remediation-rule-type-detector", "collaboration_instruction": "Picks the rule language"},
        {"agent": "remediation-splunk-rule-generator", "collaboration_instruction": "Splunk rule for a gap"},
        {
            "agent": "remediation-crowdstrike-rule-generator",
            "collaboration_instruction": "CrowdStrike rule for a gap",
        },
        {"agent": "aev-detection-rules-generator", "collaboration_instruction": "Sigma rules for a gap"},
    ],
}
