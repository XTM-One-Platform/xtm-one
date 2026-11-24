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

"""Single-product seeded fleet M3: the OpenCRQ risk budget desk (#10593, epic #8975).

``dev-docs/ctem-fleets.md`` section 13.3. The CTEM fleets F1 to F5 need two or
three modules; this one works with OpenCRQ alone, for a CISO or a risk
manager who defends the security budget to the CFO, the Board or an insurer.
It keeps a risk register priced in money current and ranks the controls to
fund by the expected annual loss each removes per dollar.

Four new built-in agents, their assignments and the fleet entry;
:mod:`app.agents.builtin_agents` and :mod:`app.agents.builtin_fleets` extend
their lists with them.

- ``opencrq-risk-desk-orchestrator`` runs the loop: re-read the register when
  OpenCRQ re-prices a scenario, re-scores a finding or ingests a control
  validation, have the movement explained, rank the next control investments
  and produce the monthly Board pack.
- ``opencrq-register-analyst`` explains what moved in the register and why:
  each scenario re-priced, its expected annual loss before and after, and the
  driver (frequency, magnitude, a control) (``risk_scenario_quantified``).
- ``opencrq-investment-analyst`` runs the what-if of the candidate control
  upgrades and writes one investment case per control: the loss removed, the
  cost, the loss removed per dollar (``investment_case_written``).
- ``opencrq-board-reporter`` writes the monthly Board pack: the expected
  annual loss trend, the top controls by loss removed and the progress of the
  treatment plans and budget reviews (``briefing_written``).
- ``opencrq-assistant`` and ``ctem-risk-quantifier`` are connected by slug,
  never moved, renamed or changed.

OpenCRQ's own threat catalogue is the source: the fleet has no dependency on
OpenCTI. Treatment plans and budget reviews are read only: the fleet
recommends, a person decides and writes them in OpenCRQ. A write to the
register of record (a scenario, its loss estimates, the risk tolerance) is
proposed by the personas as a ``needs_human`` work item, and the fleet's
``opencrq.register.write`` gate (impact high) logs any such call. The
fleet is admin opt-in and off by default like every seeded fleet
(``SEEDED_FLEET_OPT_IN``, #9160): nothing runs or meters quota until an
administrator switches an assignment on.
"""

from __future__ import annotations

from typing import Any

from app.agents.fleet_opt_in import SEEDED_FLEET_OPT_IN
from app.agents.gate_answered import gate_answered_assignment
from app.services import opencrq_job_events

ORCHESTRATOR = "opencrq-risk-desk-orchestrator"
REGISTER_ANALYST = "opencrq-register-analyst"
INVESTMENT_ANALYST = "opencrq-investment-analyst"
BOARD_REPORTER = "opencrq-board-reporter"
FLEET_SLUG = "opencrq-risk-budget-desk"
FLEET_NAME = "Risk budget desk"

#: Shared state of the fleet: the last register snapshot, the ranked investments, the Board packs.
STATE_KEY = "opencrq:risk-budget-desk:state"
#: Where the monthly snapshots trimmed from STATE_KEY (keep_last) go: history is archived, never lost.
STATE_ARCHIVE_KEY = f"{STATE_KEY}:archive"
#: Monthly snapshots kept in STATE_KEY: two years of trend.
SNAPSHOTS_KEPT = 24
#: Work-item key prefix of the recommendations the fleet files for a person.
RECOMMENDATION_PREFIX = "opencrq:risk-budget-desk:recommendation:"
#: Work-item key prefix of a proposed write to the register of record (a scenario, its loss
#: estimates, the risk tolerance), followed by the write kind and the OpenCRQ id.
APPROVAL_PREFIX = "opencrq:risk-budget-desk:approval:"
#: Every decision key the desk files for a person: the "Gate answered" listener takes these.
DECISION_PREFIXES: tuple[str, ...] = (RECOMMENDATION_PREFIX, APPROVAL_PREFIX)

#: Existing agents the fleet works with; connected, never members.
SHARED_AGENTS: tuple[str, ...] = ("opencrq-assistant", "ctem-risk-quantifier")

#: The OpenCRQ events (``services/opencrq_job_events.py``) that re-read the register, by assignment.
REGISTER_EVENTS: dict[str, str] = {
    "Scenario re-priced": "risk_scenario_recomputed",
    "Finding re-scored": "finding_score_changed",
    "Control validation ingested": "control_validation_ingested",
}

#: A register move below this share of the last snapshot's total is noise (a
#: Monte Carlo re-run): no explanation, no snapshot, no investment case (#1417).
MATERIAL_MOVE_PERCENT = 1.0

#: What drove a scenario's expected annual loss to move.
DRIVERS: tuple[str, ...] = ("frequency", "magnitude", "control", "asset", "settings", "unknown")

_TARGETS = ["xtm_one", "dev"]

_REGISTER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "headline": {"type": "string", "description": "What moved in the register, in one sentence."},
        "currency": {"type": "string"},
        "total_ale_before": {"type": "number", "description": "Register expected annual loss before."},
        "total_ale_after": {"type": "number", "description": "Register expected annual loss now."},
        "scenarios": {
            "type": "array",
            "description": "One entry per scenario re-priced since the last snapshot; each counts once.",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "opencrq_id": {"type": "string"},
                    "ale_before": {"type": "number"},
                    "ale_after": {"type": "number"},
                    "driver": {"type": "string", "enum": list(DRIVERS)},
                    "why": {"type": "string", "description": "The change in OpenCRQ that explains the move."},
                    "above_tolerance": {"type": "boolean"},
                },
                "required": ["name", "ale_after", "driver", "why"],
            },
        },
        "missing_context": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["headline", "scenarios", "missing_context"],
}

_INVESTMENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "headline": {"type": "string", "description": "The control to fund first, and why, in one sentence."},
        "currency": {"type": "string"},
        "cases": {
            "type": "array",
            "description": "One investment case per control upgrade, ranked by loss removed per dollar.",
            "items": {
                "type": "object",
                "properties": {
                    "control": {"type": "string"},
                    "upgrade": {"type": "string", "description": "Strength, coverage or reliability target."},
                    "scenarios": {"type": "array", "items": {"type": "string"}},
                    "ale_removed": {"type": "number", "description": "Expected annual loss removed (what-if)."},
                    "cost": {"type": "number", "description": "Year-one cost, when OpenCRQ or a plan holds it."},
                    "loss_removed_per_dollar": {"type": "number"},
                    "treatment_plan": {"type": "string", "description": "The OpenCRQ plan already covering it."},
                    "assumptions": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["control", "ale_removed", "assumptions"],
            },
        },
        "missing_context": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["headline", "cases", "missing_context"],
}

_BOARD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "headline": {"type": "string", "description": "The month's risk position in one sentence."},
        "period": {"type": "string"},
        "currency": {"type": "string"},
        "ale_trend": {
            "type": "array",
            "description": "Register expected annual loss per month, oldest first.",
            "items": {
                "type": "object",
                "properties": {"month": {"type": "string"}, "ale": {"type": "number"}},
                "required": ["month", "ale"],
            },
        },
        "top_controls": {"type": "array", "items": {"type": "string"}},
        "plan_progress": {"type": "array", "items": {"type": "string"}},
        "budget_reviews": {"type": "array", "items": {"type": "string"}},
        "asks": {"type": "array", "items": {"type": "string"}, "description": "The decisions the Board is asked."},
        "missing_context": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["headline", "ale_trend", "top_controls", "plan_progress", "missing_context"],
}

_RUN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "scenarios_moved": {"type": "array", "items": {"type": "string"}},
        "investment_cases": {"type": "array", "items": {"type": "string"}},
        "board_pack": {"type": "string"},
        "recommendations": {"type": "array", "items": {"type": "string"}},
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
    "integration_template_bindings": ["opencrq"],
    # The three roles read the register and recommend; a person writes it in
    # OpenCRQ. Their OpenCRQ binding is read-only, so no write tool is offered.
    "integration_template_read_only": ["opencrq"],
    **_NO_MEMORY,
}

_DELEGATES = (REGISTER_ANALYST, INVESTMENT_ANALYST, BOARD_REPORTER, "ctem-risk-quantifier")

RISK_BUDGET_DESK_AGENTS: list[dict[str, Any]] = [
    {
        "slug": ORCHESTRATOR,
        "persona": ORCHESTRATOR,
        "name": "Risk Desk Orchestrator",
        "description": (
            "Orchestrates the risk budget desk: keeps the OpenCRQ risk register priced and current, "
            "explains what moved, ranks the next control investments by loss removed per dollar and "
            "produces the monthly Board pack."
        ),
        "icon": "Landmark",
        "output_format": "json",
        "output_schema": _RUN_SCHEMA,
        # A run orchestrator (JSON run schema): driven by its assignments, never a chat.
        "disable_chat": True,
        "max_iterations": 150,
        "temperature": 0.2,
        "tags": ["builtin", "opencrq", "risk", "fleet", "risk-budget-desk"],
        "platform_targets": _TARGETS,
        "builtin_tool_ids": ["work_items", "shared_state", "generate_file"],
        "integration_template_bindings": ["opencrq"],
        "handover_agent_slugs": list(_DELEGATES),
        "handover_type_slugs": dict.fromkeys(_DELEGATES, "delegate"),
        **_NO_MEMORY,
    },
    {
        **_ROLE_BASE,
        "slug": REGISTER_ANALYST,
        "persona": REGISTER_ANALYST,
        "name": "Register Analyst",
        "description": (
            "Explains what moved in the OpenCRQ risk register and why: each scenario re-priced, its "
            "expected annual loss before and after, and the change that drove it."
        ),
        "icon": "TrendingUp",
        "output_schema": _REGISTER_SCHEMA,
        "tags": ["builtin", "opencrq", "risk", "fleet"],
        "builtin_tool_ids": ["shared_state"],
    },
    {
        **_ROLE_BASE,
        "slug": INVESTMENT_ANALYST,
        "persona": INVESTMENT_ANALYST,
        "name": "Investment Analyst",
        "description": (
            "Runs the OpenCRQ what-if of the candidate control upgrades and writes one investment case "
            "per control: the expected annual loss it removes, its cost and the loss removed per dollar."
        ),
        "icon": "Calculator",
        "output_schema": _INVESTMENT_SCHEMA,
        "tags": ["builtin", "opencrq", "risk", "fleet"],
        "builtin_tool_ids": [],
    },
    {
        **_ROLE_BASE,
        "slug": BOARD_REPORTER,
        "persona": BOARD_REPORTER,
        "name": "Board Reporter",
        "description": (
            "Writes the monthly Board pack: the expected annual loss trend, the top controls by loss "
            "removed and the progress of the treatment plans and budget reviews."
        ),
        "icon": "BarChart3",
        "output_schema": _BOARD_SCHEMA,
        "tags": ["builtin", "opencrq", "risk", "fleet"],
        "builtin_tool_ids": ["shared_state", "generate_file"],
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

_EVENT_PROMPT = (
    "OpenCRQ finished the jobs named in the trigger information below ({what}); `covers` lists every "
    "job this one run stands for, `run_count` how many.\n"
    "Run ONE register refresh of the risk budget desk for all of them. First read the register total "
    f"and compare it with the last snapshot in shared state {STATE_KEY}: when the total moved by less than "
    f"{MATERIAL_MOVE_PERCENT:g}% and no scenario crossed the risk tolerance, stop there and answer the run "
    "JSON with the summary 'no material move', writing nothing. Otherwise delegate to the Register Analyst "
    "the scenarios re-priced since the last snapshot, with the driver of each move, and record the "
    "snapshot keyed by the current month (update the entry of that month in place, never a second one; "
    f"a new month is appended with keep_last {SNAPSHOTS_KEPT} and archive_key {STATE_ARCHIVE_KEY}). "
    "When a scenario crosses the risk tolerance, have the Investment Analyst price the control upgrades "
    "that bring it back and file one recommendation work item. A write the register needs (a scenario, its "
    "loss estimates, the risk tolerance) is proposed for a person as one needs_human work item keyed "
    f"{APPROVAL_PREFIX} followed by the write kind and the OpenCRQ id; never write it yourself. Answer "
    "with the run JSON."
)

RISK_BUDGET_DESK_ASSIGNMENTS: list[dict[str, Any]] = [
    *(
        {
            **_COMMON,
            "name": name,
            "description": (
                f"OpenCRQ {what}: re-read the risk register, explain what moved and why, and price the "
                "fix of a scenario that crossed the tolerance."
            ),
            "trigger_type": "integration_event",
            "trigger_config": {
                "provider": opencrq_job_events.PROVIDER,
                "event": event,
                # One refresh at a time per event: a burst of OpenCRQ jobs folds
                # into the one queued run (event_coalescing, #1417), so two runs
                # never read the same base and write two snapshots.
                "max_concurrent_runs": 1,
                # The poller already asks for these runs; the filter keeps a run
                # from reaching the desk when another source replays the event.
                "filter": {
                    "entity.status": opencrq_job_events.SUCCEEDED,
                    "entity.kind": list(opencrq_job_events.EVENT_KINDS[event]),
                },
            },
            "prompt": _EVENT_PROMPT.format(what=what),
        }
        for name, event, what in (
            ("Scenario re-priced", REGISTER_EVENTS["Scenario re-priced"], "re-priced a risk scenario"),
            ("Finding re-scored", REGISTER_EVENTS["Finding re-scored"], "re-scored the findings"),
            (
                "Control validation ingested",
                REGISTER_EVENTS["Control validation ingested"],
                "ingested a control validation",
            ),
        )
    ),
    {
        **_COMMON,
        "name": "Weekly investment review",
        "description": (
            "Every Monday, the next control investments ranked by the expected annual loss each removes "
            "per dollar, against the treatment plans already open."
        ),
        "trigger_type": "schedule",
        # Staggered off the Monday 07:00Z of the PIR answer (#1832).
        "trigger_config": {"cron": "30 7 * * 1"},
        "prompt": (
            "Weekly investment review. Delegate to the Investment Analyst the what-if of the control "
            "upgrades on the scenarios with the highest expected annual loss, ranked by loss removed per "
            "dollar, and the comparison with the open OpenCRQ treatment plans and budget reviews (read "
            f"only). File one recommendation work item, keyed {RECOMMENDATION_PREFIX} followed by the "
            "control's OpenCRQ id, per control "
            "worth funding that no plan covers. Answer with the run JSON."
        ),
    },
    {
        **_COMMON,
        "name": "Monthly Board pack",
        "description": (
            "On the 1st of the month, the Board pack: expected annual loss trend, top controls by loss "
            "removed, treatment plan and budget progress."
        ),
        "trigger_type": "schedule",
        # After the monthly exposure briefing (0 8 1 * *), before the CTEM board report (#1832).
        "trigger_config": {"cron": "0 10 1 * *"},
        "prompt": (
            "Monthly Board pack. Delegate to the Board Reporter the pack of the last month: the register's "
            f"expected annual loss trend from shared state {STATE_KEY} and OpenCRQ, the top controls by loss "
            "removed from the latest investment cases, the progress of each treatment plan and the budget "
            "reviews, and the decisions to ask. Answer with the run JSON."
        ),
    },
    gate_answered_assignment(
        _COMMON,
        prefixes=DECISION_PREFIXES,
        inherit_switch_from="Weekly investment review",
        description=(
            "A person answered a control investment recommendation or a proposed register write: "
            "record the decision and track the write."
        ),
        apply=(
            f"A key starting {RECOMMENDATION_PREFIX}: record the decision on the control in shared state "
            f"{STATE_KEY} (funded, refused or deferred, with the reason), so the next review and the Board "
            f"pack carry it. A key starting {APPROVAL_PREFIX}: an approved write is made by a person in "
            "OpenCRQ, never by you; file one follow-up work item you own, recipient the person who "
            "answered (what to set, the value, where in OpenCRQ), which a later run completes once the value "
            "reads back in OpenCRQ, and name it in the outcome; a rejected one is recorded in shared state "
            f"{STATE_KEY}. OpenCRQ stays read only. Answer with the run JSON."
        ),
    ),
]

RISK_BUDGET_DESK_FLEET: dict[str, Any] = {
    "slug": FLEET_SLUG,
    "name": FLEET_NAME,
    "description": (
        "For CISOs and risk managers running OpenCRQ alone: a risk register priced in money kept current, "
        "and the controls to fund ranked by the expected annual loss each removes per dollar."
    ),
    "icon": "Landmark",
    "platform_targets": _TARGETS,
    "orchestrator": ORCHESTRATOR,
    "members": [
        {"agent": ORCHESTRATOR, "role_label": "Orchestrator", "exposed": True},
        {"agent": REGISTER_ANALYST, "role_label": "Register", "parent": ORCHESTRATOR, "exposed": False},
        {"agent": INVESTMENT_ANALYST, "role_label": "Investments", "parent": ORCHESTRATOR, "exposed": False},
        {"agent": BOARD_REPORTER, "role_label": "Board reporting", "parent": ORCHESTRATOR, "exposed": False},
    ],
    "connected": [
        {"agent": "opencrq-assistant", "collaboration_instruction": "Answers the team's questions in chat"},
        {"agent": "ctem-risk-quantifier", "collaboration_instruction": "Prices a new exposure in loss"},
    ],
}
