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

"""Where the guided setup of the CTEM loop fleet stands, step by step (#1087, CTEM-33 slice 2).

The first-connection setup (``app.services.organization_setup_prompt``) walks
an administrator through six steps: organization, modules, ecosystem, gates,
preview, enable. Each step saves on its own; this module tells the flow, when
it opens again, which step to resume on and what each step already holds, read
from the platform as it is (the profile, the module registrations, the loop
fleet's capability slots, its autonomy level and its automations), never from
what the flow remembers.

A step is ``done`` or ``skipped`` when the administrator recorded it so for the
current setup version; otherwise ``ready`` when what it asks for is already in
place (a step left midway, or set up from its own page), else ``todo``. The
flow resumes on the first step that is neither ``done`` nor ``skipped``.

Pure functions only: the router reads the rows. Nothing here enables, binds or
saves anything.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.services.organization_setup_prompt import STEPS

#: The CTEM modules the loop works with, in the order the Modules step lists them.
LOOP_MODULES: tuple[str, ...] = ("opencti", "openaev", "opencrq")

#: What the loop cannot do while a module is missing (the Modules step's degraded mode).
DEGRADED_WITHOUT: dict[str, str] = {
    "opencti": "no threat or vulnerability signals from intelligence; cases start only from other sources",
    "openaev": "no validation or re-test; cases close on ticket evidence instead of a passing test",
    "opencrq": "no risk figures; the scope gate reads crown jewels only and decisions carry no expected loss",
}

STEP_DONE = "done"
STEP_SKIPPED = "skipped"
STEP_READY = "ready"
STEP_TODO = "todo"

#: The slot capabilities the Ecosystem step lists, in order (``fleet_capability_slots.CAPABILITIES`` keys).
ECOSYSTEM_CAPABILITIES: tuple[str, ...] = ("edr_xdr", "ticketing", "scanner", "notification")

_SLOT_RANK = {"filled": 2, "empty": 1, "unavailable": 0}


@dataclass(frozen=True)
class ProfileFacts:
    """What the Organization step needs from the saved profile."""

    saved: bool = False
    crown_jewels: int = 0
    tech_stack: int = 0
    ticketing_target: bool = False
    scope_min_criticality: str = ""


@dataclass(frozen=True)
class LoopFleetFacts:
    """What the Gates, Preview and Enable steps need from the CTEM loop fleet."""

    fleet_id: str | None = None
    autonomy_level: str = ""
    #: The ``fleet_views.opt_in_state`` badge: ``on``, ``partial``, ``paused``, ``not_set_up``.
    opt_in: str = "not_set_up"
    automations_total: int = 0
    automations_enabled: int = 0
    #: The gates waiting for a person at the fleet's level, by key.
    waiting_gates: tuple[str, ...] = ()


@dataclass(frozen=True)
class StepStatus:
    key: str
    state: str
    facts: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SetupStatus:
    steps: list[StepStatus]
    #: The step the flow opens on; ``None`` when every step is done or skipped.
    resume_on: str | None


def external_slot_state(bound: Sequence[Any], candidates: Sequence[Any]) -> str:
    """A slot's state from its integrations alone: a built-in option (#1201) does not count."""
    if bound:
        return "filled"
    return "empty" if candidates else "unavailable"


def slot_summary(member_slots: Iterable[Iterable[Mapping[str, Any]]]) -> dict[str, str]:
    """The best state of each ecosystem capability across the fleet's members.

    *member_slots* is, per member, its slots as ``{capability, state}`` (the
    ``fleet_capability_slots.SlotState`` fields). A capability no member's
    role declares reads ``unavailable``.
    """
    best: dict[str, str] = dict.fromkeys(ECOSYSTEM_CAPABILITIES, "unavailable")
    for slots in member_slots:
        for slot in slots:
            capability = str(slot.get("capability") or "")
            state = str(slot.get("state") or "")
            if capability in best and _SLOT_RANK.get(state, -1) > _SLOT_RANK[best[capability]]:
                best[capability] = state
    return best


def _live_state(ready: bool) -> str:
    return STEP_READY if ready else STEP_TODO


def setup_status(
    *,
    recorded: Mapping[str, str],
    profile: ProfileFacts,
    modules: Sequence[str],
    slots: Mapping[str, str],
    fleet: LoopFleetFacts,
) -> SetupStatus:
    """Each step's state and facts, and the step to resume on.

    *recorded* is ``organization_setup_prompt.steps_of`` (step -> ``done`` or
    ``skipped``, current version only); *modules* the CTEM modules with an
    active registration; *slots* :func:`slot_summary`.
    """
    connected = [m for m in LOOP_MODULES if m in set(modules)]
    live: dict[str, tuple[bool, dict[str, Any]]] = {
        "organization": (
            profile.saved and profile.crown_jewels > 0 and profile.tech_stack > 0,
            {
                "saved": profile.saved,
                "crown_jewels": profile.crown_jewels,
                "tech_stack": profile.tech_stack,
                "ticketing_target": profile.ticketing_target,
                "scope_min_criticality": profile.scope_min_criticality,
            },
        ),
        "modules": (
            bool(connected),
            {
                "connected": connected,
                "missing": {m: DEGRADED_WITHOUT[m] for m in LOOP_MODULES if m not in connected},
            },
        ),
        "ecosystem": (
            any(state == "filled" for state in slots.values()),
            {"slots": {c: slots.get(c, "unavailable") for c in ECOSYSTEM_CAPABILITIES}},
        ),
        "gates": (
            fleet.fleet_id is not None and bool(fleet.autonomy_level),
            {"autonomy_level": fleet.autonomy_level, "waiting_gates": list(fleet.waiting_gates)},
        ),
        "preview": (
            fleet.fleet_id is not None,
            {"fleet_id": fleet.fleet_id, "automations_total": fleet.automations_total},
        ),
        "enable": (
            fleet.opt_in == "on",
            {
                "opt_in": fleet.opt_in,
                "automations_enabled": fleet.automations_enabled,
                "automations_total": fleet.automations_total,
            },
        ),
    }
    steps: list[StepStatus] = []
    resume_on: str | None = None
    for key in STEPS:
        ready, facts = live[key]
        mark = recorded.get(key)
        state = mark if mark in (STEP_DONE, STEP_SKIPPED) else _live_state(ready)
        if resume_on is None and state not in (STEP_DONE, STEP_SKIPPED):
            resume_on = key
        steps.append(StepStatus(key=key, state=state, facts=facts))
    return SetupStatus(steps=steps, resume_on=resume_on)
