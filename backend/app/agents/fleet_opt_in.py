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

"""Seeded fleet automations are opt-in (#9160, epic #8975).

A seeded automation never ships enabled; the customer opts in. Agentic runs
are metered against a customer's quota, so nothing the platform seeds for a
fleet may run, provision an OpenCTI or OpenAEV trigger, or meter quota until
an administrator switches it on:

* every fleet assignment carries :data:`SEEDED_FLEET_OPT_IN`: created disabled
  with the ``created_disabled`` stamp ("awaiting an administrator"), never
  re-enabled by a boot (``seed.seed_builtin_assignments``), and enabling it
  anchors its schedule at that instant (``mark_schedule_resumed``), so no
  catch-up run fires;
* a built-in fleet is ACTIVE only while its orchestrator holds at least one
  enabled assignment (``app.services.fleet_runs``). An inactive fleet
  changes nothing for its members' runs (no organization block, no fleet run
  defaults, no fleet trace keys) and gives the CTEM Assistant no handover to
  its orchestrator.

:data:`ENABLED_BY_DEFAULT_ALLOWLIST` names the only built-in assignments that
ship enabled: the CTEM dashboard and indicator decay automations of
1.261102.0. ``tests/test_ctem_fleets_opt_in.py`` fails on any other.

Once a fleet is on, nothing changes for what existed before it (#9188):

* a fleet automation runs and provisions its triggers as the administrator
  who switched it on (``app.services.fleet_run_as``), never as the seed owner;
* the agents of 1.261102.0 folded into a fleet as members
  (:data:`PRE_EXISTING_FLEET_MEMBERS`) get the organization block and the fleet
  run defaults only on a run the fleet started (its orchestrator caused it),
  and never on a run of the allowlisted automations
  (``fleet_runs.assignment_fleet_context``).
"""

from __future__ import annotations

#: ``trigger_config`` keys the trigger provisioning writes (the ids of the
#: OpenCTI / OpenAEV triggers and notifiers it created). The seed keeps them
#: when it rewrites a built-in's trigger_config, or a disabled assignment
#: would lose the ids the next provisioning tick needs to remove them.
PROVISIONING_STATE_KEYS: tuple[str, ...] = ("opencti_provisioning", "openaev_provisioning", "jira_webhook")

#: Spread into every seeded fleet assignment.
SEEDED_FLEET_OPT_IN: dict = {"enabled": False, "admin_opt_in": True}

#: (agent slug, stage key) of the five CTEM stage analysts.
_STAGES = (
    ("ctem-scoping-analyst", "scoping"),
    ("ctem-discovery-analyst", "discovery"),
    ("ctem-prioritization-analyst", "prioritization"),
    ("ctem-validation-analyst", "validation"),
    ("ctem-mobilization-analyst", "remediation"),
)

#: (agent slug, assignment name) of the 14 dashboard and decay automations
#: of 1.261102.0, the only built-in assignments allowed to ship enabled (the
#: hourly decay re-check is listed but already opt-in, off by default).
#: Adding one is a product decision, never a fix.
ENABLED_BY_DEFAULT_ALLOWLIST: frozenset[tuple[str, str]] = frozenset(
    {
        *((slug, f"Weekly {key} assessment") for slug, key in _STAGES),
        *((slug, f"Generate my {key} assessment") for slug, key in _STAGES),
        ("exposure-briefing-analyst", "Monthly exposure briefing"),
        ("exposure-briefing-analyst", "Generate my exposure briefing"),
        ("indicator-decay-guardian", "Re-check indicators about to decay"),
        ("indicator-decay-guardian", "Hourly indicator decay re-check"),
    }
)

#: Built-in agents of 1.261102.0 that a seeded fleet folds in as members. A
#: run of theirs nobody in the fleet started resolves as in 1.261102.0.
PRE_EXISTING_FLEET_MEMBERS: frozenset[str] = frozenset(
    {
        "openaev-scenario-creator",
        "opencti-threat-hunt-creator",
        "opencti-detection-rule-converter",
        *(slug for slug, _key in _STAGES),
        "exposure-briefing-analyst",
    }
)
