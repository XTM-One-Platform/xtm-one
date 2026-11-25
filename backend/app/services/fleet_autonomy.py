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

"""Fleet autonomy level (#9769, slice S1): how many of a fleet's actions wait for a person.

A fleet stores one level (``Fleet.autonomy_level``), one of :data:`AUTONOMY_LEVELS`
from the most supervised to the least:

* ``full_gates`` (the default of every new and seeded fleet, #2279): every
  gated action waits for a decision item between steps; no tool call pauses
  unless an administrator opted the fleet in (#1467, never a built-in one,
  #1675). ``app.agents.fleet_seed.SEEDED_FLEET_AUTONOMY_LEVEL`` names it.
* ``medium``: low-impact writes go without approval; high-impact and
  destructive actions still wait.
* ``low``: only destructive or production-impacting actions wait.
* ``autonomous``: nothing waits; every action is still logged and reported.

Each gated action a fleet declares carries an impact class
(:data:`GATE_IMPACTS`); :func:`is_gated` is the single matrix that decides,
from the level and the impact, whether that action waits. It fails closed: an
unknown level or impact is gated.

Who may move the level (:func:`ensure_level_change_allowed`): lowering autonomy
(towards ``full_gates``) is open to whoever manages the fleet, a tool included;
raising it is reserved to a human administrator.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any, Literal

from app.services.fleets import FleetActionRefusedError

AutonomyLevel = Literal["full_gates", "medium", "low", "autonomous"]
GateImpact = Literal["low", "high", "destructive"]

#: The levels, from the most supervised to the least.
AUTONOMY_LEVELS: tuple[str, ...] = ("full_gates", "medium", "low", "autonomous")
DEFAULT_AUTONOMY_LEVEL = "full_gates"

#: Impact classes of a gated action, from the mildest to the most severe.
#: ``low``: a reversible write of small reach (a ticket, a promoted draft, a
#: re-test of an approved scenario). ``high``: a write that acts on the
#: environment (a simulation launched, a rule deployed). ``destructive``: a
#: deletion or an action on production assets.
GATE_IMPACTS: tuple[str, ...] = ("low", "high", "destructive")

#: The mildest impact that still waits at each level; ``None`` = nothing waits.
_GATED_FROM: dict[str, str | None] = {
    "full_gates": "low",
    "medium": "high",
    "low": "destructive",
    "autonomous": None,
}

AUTONOMY_RAISE_REFUSAL = "Only an administrator can raise a fleet's autonomy level; lowering it is open."

#: Per-action overrides of the level (#10153, slice B2 of #10000), stored in
#: ``Fleet.approval_pins`` as ``{gate key: pin}``: ``wait`` makes the gate wait
#: at every level, ``run`` lets it run alone at every level. A gate without a
#: pin follows the level.
PIN_WAIT = "wait"
PIN_RUN = "run"
APPROVAL_PINS: tuple[str, ...] = (PIN_WAIT, PIN_RUN)
PIN_RUN_REFUSAL = "Only an administrator can let an action always run alone or remove an 'Always wait' pin."


#: Where a fleet's instructions (persona, guardrails, skills) put it on the
#: same four-level scale, between-step positions included (#10168, B2-I of
#: #10000), from the most supervised to the least. Never a platform gate: the
#: page shows it as "Set by the fleet's instructions, not enforced by the
#: platform", and the members are told it once a person moved it.
INSTRUCTIONS_POSITIONS: tuple[str, ...] = (
    "full_gates",
    "full_gates_medium",
    "medium",
    "medium_low",
    "low",
    "low_autonomous",
    "autonomous",
)
INSTRUCTIONS_POSITION_LABELS: dict[str, str] = {
    "full_gates": "Full human gates",
    "full_gates_medium": "between Full human gates and Medium",
    "medium": "Medium",
    "medium_low": "between Medium and Low",
    "low": "Low",
    "low_autonomous": "between Low and Full autonomous",
    "autonomous": "Full autonomous",
}
INSTRUCTIONS_RAISE_REFUSAL = (
    "Only an administrator can move a fleet's instructions position toward more autonomy; lowering it is open."
)


def normalize_instructions_position(value: Any) -> str | None:
    """*value* as a stored instructions position (``None`` clears it); refused (422) when unknown."""
    if value is None:
        return None
    if isinstance(value, str) and value in INSTRUCTIONS_POSITIONS:
        return value
    raise FleetActionRefusedError(
        422, f"Unknown instructions position '{value}': one of {', '.join(INSTRUCTIONS_POSITIONS)}."
    )


def ensure_instructions_change_allowed(*, previous: Any, new: Any, is_admin: bool) -> None:
    """Refuse (403) a move toward more autonomy by anyone but an administrator.

    No position reads as the most supervised one, so setting a first position
    is a raise unless it is ``full_gates``: the same rule as the level.
    """
    before = INSTRUCTIONS_POSITIONS.index(previous) if previous in INSTRUCTIONS_POSITIONS else 0
    after = INSTRUCTIONS_POSITIONS.index(new) if new in INSTRUCTIONS_POSITIONS else 0
    if after > before and not is_admin:
        raise FleetActionRefusedError(403, INSTRUCTIONS_RAISE_REFUSAL)


def instructions_line(position: Any, announced: Any) -> str | None:
    """The run prompt line of an announced instructions position; ``None`` otherwise (display only)."""
    if not announced or position not in INSTRUCTIONS_POSITIONS:
        return None
    return (
        f"Your instructions (persona, guardrails, skills) set this fleet at "
        f"{INSTRUCTIONS_POSITION_LABELS[position]}: apply the approvals they name; the platform does not "
        "enforce this part."
    )


def normalize_level(value: Any) -> str:
    """*value* as a stored level; refused (422) when it is not one of :data:`AUTONOMY_LEVELS`."""
    if isinstance(value, str) and value in AUTONOMY_LEVELS:
        return value
    raise FleetActionRefusedError(422, f"Unknown autonomy level '{value}': one of {', '.join(AUTONOMY_LEVELS)}.")


def effective_level(value: Any) -> str:
    """The level a stored value stands for: an unknown or empty value reads as the default (fail closed)."""
    return value if isinstance(value, str) and value in AUTONOMY_LEVELS else DEFAULT_AUTONOMY_LEVEL


def is_gated(level: Any, impact: Any) -> bool:
    """Whether an action of *impact* waits for a person at *level*.

    Fails closed: an unknown level reads as ``full_gates`` and an unknown
    impact as ``destructive``, so a typo never removes a gate.
    """
    threshold = _GATED_FROM[effective_level(level)]
    if threshold is None:
        return False
    rank = GATE_IMPACTS.index(impact) if impact in GATE_IMPACTS else len(GATE_IMPACTS) - 1
    return rank >= GATE_IMPACTS.index(threshold)


def gate_waits(level: Any, gate: Mapping[str, Any]) -> bool:
    """Whether *gate* waits for a person at *level*, its pin (``gate['pin']``) first."""
    pin = gate.get("pin")
    if pin == PIN_WAIT:
        return True
    if pin == PIN_RUN:
        return False
    return is_gated(level, gate.get("impact") or "high")


def clean_pins(pins: Any) -> dict[str, str]:
    """The valid entries of a stored ``approval_pins`` value; anything else is dropped."""
    if not isinstance(pins, Mapping):
        return {}
    return {str(k): str(v) for k, v in pins.items() if k and v in APPROVAL_PINS}


def pins_lower_approval(previous: Mapping[str, str], new: Mapping[str, str]) -> bool:
    """Whether moving from the pins *previous* to *new* lets an action run with less approval.

    A new ``run`` pin, or a ``wait`` pin removed or turned to ``run``: the same
    rule as raising the level, an administrator only.
    """
    for key in set(previous) | set(new):
        before, after = previous.get(key), new.get(key)
        if after == PIN_RUN and before != PIN_RUN:
            return True
        if before == PIN_WAIT and after != PIN_WAIT:
            return True
    return False


def is_raise(previous: Any, new: Any) -> bool:
    """Whether moving from *previous* to *new* makes the fleet more autonomous."""
    return AUTONOMY_LEVELS.index(effective_level(new)) > AUTONOMY_LEVELS.index(effective_level(previous))


def ensure_level_change_allowed(*, previous: Any, new: Any, is_admin: bool) -> None:
    """Refuse (403) a raise by anyone but a human administrator; a lowering or no change passes."""
    if is_raise(previous, new) and not is_admin:
        raise FleetActionRefusedError(403, AUTONOMY_RAISE_REFUSAL)


def gate_catalog(level: Any, gates: Iterable[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    """The fleet's declared gates, each with ``gated`` at *level* and the same flag at every level.

    *gates* is the ``gates`` list of the fleet's structured documentation
    (``app.services.fleet_docs.DocGate``). ``by_level`` lets the reader show, per
    step of the control, which actions become automatic, so the level is never a
    black box.
    """
    out: list[dict[str, Any]] = []
    for gate in gates or ():
        impact = gate.get("impact") or "high"
        out.append(
            {
                "key": gate.get("key") or "",
                "action": gate.get("action") or "",
                "module": gate.get("module") or "platform",
                "provider": gate.get("provider") or "",
                "impact": impact,
                "source": gate.get("source") or "stored",
                "pin": gate.get("pin") if gate.get("pin") in APPROVAL_PINS else None,
                "tools": [
                    str(t["name"]) for t in gate.get("tools") or () if isinstance(t, Mapping) and t.get("name")
                ],
                "gated": gate_waits(level, gate),
                "by_level": {lvl: gate_waits(lvl, gate) for lvl in AUTONOMY_LEVELS},
            }
        )
    return out


def with_live_step_gates(structured: Any, catalog: Iterable[Mapping[str, Any]] | None) -> Any:
    """*structured* with each step's ``gate`` read from the gate it names (#1074).

    A step whose ``gate_key`` is in *catalog* (:func:`gate_catalog`, the fleet's
    live level and pins applied) shows its approval badge exactly when that gate
    waits, so the About page and the Approval tab never disagree. A step without
    a key, or naming a gate the catalog does not hold, keeps its stored flag.
    The stored value is never mutated: a copy is returned when a step changes.
    """
    if not isinstance(structured, Mapping) or not isinstance(structured.get("steps"), list):
        return structured
    waits = {str(g.get("key")): bool(g.get("gated")) for g in catalog or () if isinstance(g, Mapping) and g.get("key")}
    steps: list[Any] = []
    for step in structured["steps"]:
        key = step.get("gate_key") if isinstance(step, Mapping) else None
        steps.append(dict(step, gate=waits[key]) if key in waits else step)
    return {**structured, "steps": steps}


# ── Run time (#9769, slice S1b) ─────────────────────────────────────────────


def _structured_gates(documentation: Any) -> list[Mapping[str, Any]]:
    """The ``gates`` of a fleet's stored structured documentation; ``[]`` when absent or malformed."""
    if not isinstance(documentation, Mapping):
        return []
    structured = documentation.get("structured")
    gates = structured.get("gates") if isinstance(structured, Mapping) else None
    return [g for g in gates if isinstance(g, Mapping)] if isinstance(gates, list) else []


def fleet_gates(approval_gates: Any, documentation: Any) -> list[Mapping[str, Any]]:
    """The gates the fleet's level acts on (#10000): the stored catalog, else the documentation's.

    ``Fleet.approval_gates`` is configuration (the seed, a fork, a person): a
    documentation regenerate never writes it. A fleet without one (``None``)
    keeps reading the gates of its documentation, today's behaviour; a stored
    empty list means the fleet gates nothing through this control.
    """
    if isinstance(approval_gates, list):
        return [g for g in approval_gates if isinstance(g, Mapping)]
    return _structured_gates(documentation)


def customer_gates_of(value: Any) -> list[Mapping[str, Any]]:
    """The valid entries of a stored ``Fleet.customer_gates`` value (#916): the gates a person added to a built-in fleet."""
    return [g for g in value if isinstance(g, Mapping) and g.get("key")] if isinstance(value, list) else []


def effective_gates(
    approval_gates: Any,
    documentation: Any,
    computed: Iterable[Mapping[str, Any]] | None = None,
    pins: Any = None,
    customer: Any = None,
) -> list[dict[str, Any]]:
    """The gates the level acts on (#10153 C1): the declared ones, then the computed ones, each with its pin.

    The declared gates are :func:`fleet_gates` (stored catalog, else the
    documentation's); *computed* are the gates computed from the members'
    configuration (``fleet_gate_proposals.propose_gates``, custom fleets only),
    added when no declared gate has the same key, so accepting a proposal
    never changes what waits. ``source`` says where each gate comes from
    (``stored``, ``documentation``, ``customer``, ``computed``); ``pin`` is the
    stored per-action override of :func:`clean_pins`, when there is one.
    *customer* is ``Fleet.customer_gates`` (#916): gates a person added to a
    built-in fleet, read after its seeded ones; a key the seed declares keeps
    the seeded gate (a pin moves that one).
    """
    declared_source = "stored" if isinstance(approval_gates, list) else "documentation"
    out: list[dict[str, Any]] = [dict(g, source=declared_source) for g in fleet_gates(approval_gates, documentation)]
    keys = {str(g.get("key") or "") for g in out}
    for gate in customer_gates_of(customer):
        if str(gate.get("key")) not in keys:
            out.append(dict(gate, source="customer"))
            keys.add(str(gate.get("key")))
    for gate in computed or ():
        if isinstance(gate, Mapping) and str(gate.get("key") or "") not in keys:
            out.append(dict(gate, source="computed"))
            keys.add(str(gate.get("key") or ""))
    cleaned = clean_pins(pins)
    for gate in out:
        gate.pop("pin", None)
        pin = cleaned.get(str(gate.get("key") or ""))
        if pin:
            gate["pin"] = pin
    return out


#: The integration context key holding :func:`gate_tool_names` of the run's fleet (#1480).
FLEET_GATED_TOOLS_CONTEXT_KEY = "fleet_gated_tools"
#: The integration context key holding :func:`waiting_gate_keys` of the run's fleet (#2102).
FLEET_WAITING_GATES_CONTEXT_KEY = "fleet_waiting_gates"


def waiting_gate_keys(level: Any, gates: Iterable[Mapping[str, Any]] | None) -> frozenset[str]:
    """The keys of the fleet's gates that wait for a person at *level*, pins included (#2102).

    A tool whose safe use depends on a gate reads it from the run's integration
    context: the OpenCTI security coverage refuses a periodicity while the
    CTEM validation gate waits, so OpenAEV never schedules the scenario before
    the decision.
    """
    return frozenset(str(g.get("key")) for g in gates or () if g.get("key") and gate_waits(level, g))


def gate_tool_names(gates: Iterable[Mapping[str, Any]] | None) -> frozenset[str]:
    """Every tool a gate of the fleet names or offers, whether it waits at the level or runs (#1480).

    A tool offered only because a gate decides it (``apply_opencti_draft`` under
    the CTEM analyst review) is offered whenever its gate exists. A built-in
    gate declares no ``tools`` (#1675): it names what it offers by its key
    (``builtin_fleet_docs.GATE_OFFERED_TOOLS``), so the offer never depends on
    a tool-level pause. On a custom fleet opted in to tool-level enforcement,
    :func:`gated_tool_rules` pauses the call where the gate waits.
    """
    from app.agents.builtin_fleet_docs import GATE_OFFERED_TOOLS

    names: set[str] = set()
    for gate in gates or ():
        names.update(GATE_OFFERED_TOOLS.get(str(gate.get("key") or ""), ()))
        for tool in gate.get("tools") or ():
            if isinstance(tool, Mapping) and tool.get("name"):
                names.add(str(tool["name"]))
    return frozenset(names)


def gated_tool_rules(level: Any, gates: Iterable[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    """The tool rules the run enforces at *level*: one per tool of each gate that still waits.

    Each rule is ``{name, when, key, action}``; a gate that declares no tool is
    stated to the agent only (:func:`fleet_autonomy_block`).
    """
    rules: list[dict[str, Any]] = []
    for gate in gates or ():
        if not gate_waits(level, gate):
            continue
        grant = _grant_of(gate)
        for tool in gate.get("tools") or ():
            if not isinstance(tool, Mapping) or not tool.get("name"):
                continue
            raw_when = tool.get("when")
            when: Mapping[str, Any] = raw_when if isinstance(raw_when, Mapping) else {}
            rule: dict[str, Any] = {
                "name": str(tool["name"]),
                "when": {str(k): [str(v) for v in (vals or ())] for k, vals in when.items()},
                "key": gate.get("key") or "",
                "action": gate.get("action") or "",
            }
            if grant is not None:
                rule["grant"] = grant
            rules.append(rule)
    return rules


def _grant_of(gate: Mapping[str, Any]) -> dict[str, list[str]] | None:
    """The gate's grant (``DocGate.grant``, #1339) as ``{objects, fresh}``, or ``None`` without one."""
    grant = gate.get("grant")
    if not isinstance(grant, Mapping):
        return None
    objects = [str(a) for a in grant.get("objects") or () if a]
    if not objects:
        return None
    return {"objects": objects, "fresh": [str(a) for a in grant.get("fresh") or () if a]}


#: The object ids an approved call's answer names (an OpenAEV id is a UUID).
_OBJECT_ID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.IGNORECASE)


def _given(value: Any) -> bool:
    """Whether an argument holds a value (an empty string, list or ``None`` does not)."""
    if value is None:
        return False
    if isinstance(value, str | list | tuple | dict | set):
        return bool(value)
    return True


class _ValidationGrants:
    """One decision per validation (#1339): what a person's approval of a gated call covers.

    A call a person approved (or a call this grant let through) adds, per gate
    key, the object ids it named in the gate's ``grant.objects`` arguments and
    the ids its answer returned (the object it created). A later call of the same
    gate runs without a second pause when it names one of those objects and gives
    none of the gate's ``grant.fresh`` arguments: a new target, a new schedule or
    another object asks again. Held for the turn, never stored.
    """

    def __init__(self) -> None:
        self._objects: dict[str, set[str]] = {}
        self._passed: dict[str, str] = {}

    def covers(self, rule: Mapping[str, Any], arguments: Any) -> bool:
        grant = rule.get("grant")
        granted = self._objects.get(rule.get("key") or "")
        if not grant or not granted or not isinstance(arguments, Mapping):
            return False
        if any(_given(arguments.get(a)) for a in grant["fresh"]):
            return False
        named = {str(arguments.get(a)).lower() for a in grant["objects"] if _given(arguments.get(a))}
        return bool(named) and named <= granted

    def passed(self, call_id: str, key: str) -> None:
        self._passed[call_id] = key

    def observe(self, rules: list[dict[str, Any]], call: Any, result: Any, approved: set[str]) -> None:
        """Record what an approved (or granted) gated call named and created."""
        key = self._passed.get(call.id)
        rule = next(
            (r for r in rules if r.get("grant") and rule_matches(r, call.name, call.arguments)),
            None,
        )
        if rule is None:
            return
        if key is None and call.id in approved:
            key = rule.get("key") or ""
        if key is None or getattr(result, "is_error", False):
            return
        ids = self._objects.setdefault(key, set())
        arguments = call.arguments if isinstance(call.arguments, Mapping) else {}
        for argument in rule["grant"]["objects"]:
            if _given(arguments.get(argument)):
                ids.add(str(arguments[argument]).lower())
        content = getattr(result, "content", "")
        if isinstance(content, str):
            ids.update(m.lower() for m in _OBJECT_ID.findall(content))


def rule_matches(rule: Mapping[str, Any], name: str, arguments: Any) -> bool:
    """Whether a call of tool *name* with *arguments* is the action *rule* gates.

    Every argument the rule names must hold one of its values (compared as
    strings, case-insensitive); arguments that are not a mapping match (a
    malformed call is gated, fail closed).
    """
    if rule.get("name") != name:
        return False
    when = rule.get("when") or {}
    if not when:
        return True
    if not isinstance(arguments, Mapping):
        return True
    for argument, values in when.items():
        given = arguments.get(argument)
        if given is None or str(given).lower() not in {str(v).lower() for v in values}:
            return False
    return True


def make_fleet_autonomy_gate(inner, rules: list[dict[str, Any]], *, approved_call_ids: set[str] | None = None):
    """*inner* (the run's approval gate, may be ``None``), plus a pause on every call a gated rule matches.

    The fleet's gates are enforced here, in the tool path, whatever the model
    decides: a matching call raises ``HumanInputRequestedError`` unless a person
    approved this very call (*approved_call_ids*, the resume decisions).
    """
    approved = approved_call_ids or set()
    grants = _ValidationGrants()

    def _gate(call) -> None:
        from app.tools.base import HumanInputRequestedError

        if call.id not in approved:
            for rule in rules:
                if rule_matches(rule, call.name, call.arguments):
                    if grants.covers(rule, call.arguments):
                        # The validation a person approved: no second pause (#1339).
                        grants.passed(call.id, rule.get("key") or "")
                        continue
                    raise HumanInputRequestedError(
                        question=(
                            f"Approval required to run {call.name}: the fleet gates '{rule['action'] or rule['key']}' "
                            "at its autonomy level"
                        ),
                        tool_call_id=call.id,
                        conversation=[],
                    )
        if inner is not None:
            inner(call)

    def _observe(call: Any, result: Any) -> None:
        grants.observe(rules, call, result, approved)

    # Read by the composite after each dispatch (``CompositeToolExecutor``): the
    # answer of an approved call names the validation it created (#1339).
    _gate.observe = _observe  # type: ignore[attr-defined]
    return _gate


async def install_fleet_autonomy_gate(
    db, composite, fleet_id: Any, *, approved_call_ids: set[str] | None = None
) -> bool:
    """Wrap *composite*'s installed gate with the pauses of *fleet_id*'s gates at its level (#10140).

    Only for a fleet an administrator opted in to tool-level enforcement
    (``Fleet.enforce_gates_at_tool_level``, #1467): by default a fleet's gates
    are instructions, stated in the run prompt, and no tool call pauses.

    The entry points other than an assignment run (chat, channel threads,
    consults, sub-agents, work-item turns) call this after installing the
    agent's own gate, so a member's gated action pauses wherever it acts. The
    fleet gate only adds pauses: the inner gate keeps deciding every other
    call. The platform kill switch wins, as in an assignment run. Answers
    whether a fleet gate was installed; a fleet that gates no tool at its
    level, or an unreadable fleet, leaves the composite unchanged.
    """
    autonomy = await fleet_autonomy_for_run(db, fleet_id)
    if autonomy is None:
        return False
    if not await fleet_enforces_at_tool_level(db, fleet_id):
        return False
    rules = gated_tool_rules(*autonomy)
    if not rules:
        return False
    from app.services.hitl_whitelist import resolve_platform_kill_switch
    from app.tools.approval import OUTCOME_APPROVALS_DISABLED

    if await resolve_platform_kill_switch(db) == OUTCOME_APPROVALS_DISABLED:
        return False
    composite.set_approval_gate(
        make_fleet_autonomy_gate(composite.approval_gate, rules, approved_call_ids=approved_call_ids)
    )
    return True


LEVEL_LABELS: dict[str, str] = {
    "full_gates": "Full human gates",
    "medium": "Medium",
    "low": "Low",
    "autonomous": "Full autonomous",
}

FLEET_AUTONOMY_HEADER = "── Fleet autonomy level ──"


def fleet_autonomy_block(
    level: Any,
    gates: Iterable[Mapping[str, Any]] | None,
    instructions: str | None = None,
    *,
    enforced: bool = False,
) -> str:
    """The block appended to a fleet member's run prompt: the level and, per declared gate, waits or automatic.

    *enforced* is ``Fleet.enforce_gates_at_tool_level`` (#1467): without it the
    gates are instructions only, and the block says that an answered decision
    is not asked again.

    *instructions* is :func:`instructions_line` of the fleet: one more line
    when a person announced the instructions position, nothing otherwise, so
    the block of every fleet nobody moved is unchanged (#10168).
    """
    effective = effective_level(level)
    lines = [f"\n\n{FLEET_AUTONOMY_HEADER}", f"This fleet runs at: {LEVEL_LABELS[effective]} ({effective})."]
    catalog = gate_catalog(effective, gates)
    # A computed gate that runs alone is counted, not listed: a custom fleet
    # computes one gate per integration and impact, and the agent needs only
    # what waits (no prompt inflation).
    listed = [e for e in catalog if e["gated"] or e["source"] != "computed"]
    alone = len(catalog) - len(listed)
    if listed:
        lines.append("Gated actions of this fleet at this level:")
        for entry in listed:
            verdict = "waits for a person's approval" if entry["gated"] else "runs without approval (still logged)"
            lines.append(f"- {entry['action']} ({entry['module']}, impact {entry['impact']}): {verdict}")
    if alone:
        lines.append(f"{alone} other write action group(s) of this fleet run without approval (still logged).")
    if enforced:
        lines.append(
            "An action that waits needs a person's approval before it runs, as your instructions say; the "
            "platform also pauses the tool calls the fleet maps to it until a person approves. An action that "
            "runs without approval is still reported in the case. Only an administrator can raise this level."
        )
    else:
        lines.append(
            "An action that waits is one decision item for a person (a needs_human work item with its "
            "ask), filed between steps: never a pause inside a run. Ask once; once a person has answered "
            "that decision, you decide what it means and do it without asking again. An action that runs "
            "without approval is still reported in the case. Only an administrator can raise this level."
        )
    if instructions:
        lines.append(instructions)
    return "\n".join(lines)


async def fleet_instructions_for_run(db, fleet_id: Any) -> str | None:
    """The :func:`instructions_line` of the run's home fleet; ``None`` when not announced or on a failed read."""
    if fleet_id is None:
        return None
    import logging

    from sqlalchemy import select

    from app.models.fleet import Fleet

    try:
        async with db.begin_nested():
            row = (
                await db.execute(
                    select(Fleet.instructions_position, Fleet.instructions_position_announced).where(
                        Fleet.id == fleet_id
                    )
                )
            ).first()
    except Exception:
        logging.getLogger(__name__).exception("Fleet instructions position unreadable for fleet %s", fleet_id)
        return None
    return instructions_line(row[0], row[1]) if row is not None else None


async def fleet_enforces_at_tool_level(db: Any, fleet_id: Any) -> bool:
    """Whether an administrator opted *fleet_id* in to tool-level gate pauses (#1467); ``False`` on a failed read.

    Off by default: XTM One enables no programmatic tool-level gate unless a
    person asks for one, the same rule as an agent's ``approve_tool_calls``.
    Never on a built-in (seeded) fleet, whatever the stored flag (#1675): its
    tools are always offered and authorized, and its gates are decision items
    between steps, never a pause inside a run.
    """
    if fleet_id is None:
        return False
    import logging

    from sqlalchemy import select

    from app.models.fleet import Fleet

    try:
        async with db.begin_nested():
            row = (
                await db.execute(
                    select(Fleet.enforce_gates_at_tool_level, Fleet.is_builtin).where(Fleet.id == fleet_id)
                )
            ).first()
    except Exception:
        logging.getLogger(__name__).exception("Fleet tool-level enforcement unreadable for fleet %s", fleet_id)
        return False
    if row is None or row[1]:
        return False
    return bool(row[0])


async def fleet_autonomy_for_run(db, fleet_id: Any) -> tuple[str, list[Mapping[str, Any]]] | None:
    """The level and the effective gates of the run's home fleet; ``None`` without a fleet or on a failed read.

    The gates are :func:`effective_gates`: declared, computed from the members'
    configuration on a custom fleet (#10153 C1), with their pins. A failed read
    never fails the run, and never lowers a gate: the caller keeps its own
    gate unchanged.
    """
    if fleet_id is None:
        return None
    import logging

    from app.services.fleet_gate_proposals import fleet_effective_gates

    try:
        async with db.begin_nested():
            found = await fleet_effective_gates(db, fleet_id)
    except Exception:
        logging.getLogger(__name__).exception("Fleet autonomy unreadable for fleet %s", fleet_id)
        return None
    return found
