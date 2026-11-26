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

"""The conversational setup of a fleet (#11250, part B of #11224's onboarding).

"Create with a prompt" on the Fleets page opens a chat with the person's
default assistant that interviews them until the fleet is complete. The
assistant reads where a fleet stands with the ``get_fleet_setup`` tool of the
fleet functions; this module is its rule, a pure function of the fleet as
stored, so leaving the chat and asking again later resumes at the next step.

The steps, in order: the fleet's purpose (its description), its home members,
its orchestrator, then the prerequisites the members' configuration needs
(integrations, knowledge bases, MCP servers, agent accounts, handovers,
automations, the organization profile) as ``live_prerequisites`` checks them,
then the manual prerequisites a person confirms. Every write goes through the
existing fleet functions on the person's confirmation; this module writes
nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: The steps of the interview, in the order they are asked.
SETUP_STEPS: tuple[str, ...] = ("purpose", "members", "orchestrator", "prerequisites", "confirmations")


@dataclass(frozen=True)
class FleetSetupFacts:
    """What the setup reads of a stored fleet."""

    name: str
    description: str = ""
    home_members: int = 0
    has_orchestrator: bool = False
    #: Prerequisite lines not met (their labels), fixed outside the fleet (an integration, a knowledge base...).
    unmet: list[str] = field(default_factory=list)
    #: Manual prerequisite lines a person still confirms (their labels).
    to_confirm: list[str] = field(default_factory=list)
    #: Home members (their names) with no consult handover to the orchestrator (#11948).
    #: A suggestion only: it never holds the setup back.
    cannot_consult_orchestrator: list[str] = field(default_factory=list)
    #: Whether the fleet has no working knowledge base (#11972): one the orchestrator and every home member
    #: share, or one the orchestrator writes for them. A suggestion only: it never holds the setup back.
    without_working_knowledge: bool = False


#: The skeleton of a new working knowledge base (#11972): document title, then what goes in it.
WORKING_KNOWLEDGE_SKELETON: tuple[tuple[str, str], ...] = (
    (
        "00 - How this knowledge base works",
        "Who writes it (the orchestrator), who reads it (every member), how members propose updates (a KNOWLEDGE: line in their run result), and the numbering of the documents.",
    ),
    (
        "10 - Fleet runbook",
        "The live fleet: members and ids, accounts, assignments with their trigger, skills, shared state and memory keys, event triggers, temporary assignments with their exit criterion, a rules registry, known gaps, and a change log of the last 14 days.",
    ),
    (
        "15 - Integration routing",
        "Which integration and account does what, which member is bound to it, read-only or not.",
    ),
    ("40 - Standing preferences", "The owner's standing preferences, each dated, with who stated it."),
    ("90 - Lessons log", "Dated lines: what failed, what fixed it."),
)


def working_knowledge_name(fleet_name: str) -> str:
    """The suggested name of a fleet's working knowledge base."""
    return f"{fleet_name.strip()} - Working knowledge"


def can_consult(handover_agent_ids: object, handover_types: object, target_id: object) -> bool:
    """Whether an agent's handovers carry a consult handover to *target_id*.

    A target with no ``handover_types`` entry defaults to delegate, as the
    agent model reads it.
    """
    target = str(target_id)
    if target not in {str(t) for t in (handover_agent_ids or [])}:
        return False
    types = handover_types if isinstance(handover_types, dict) else {}
    from app.services.handover_types import handover_type_set

    return "consult" in handover_type_set(types.get(target) or "delegate")


def next_step(facts: FleetSetupFacts) -> str | None:
    """The first step of :data:`SETUP_STEPS` the fleet still needs, ``None`` when it is complete."""
    if not facts.description.strip():
        return "purpose"
    if facts.home_members <= 0:
        return "members"
    if not facts.has_orchestrator:
        return "orchestrator"
    if facts.unmet:
        return "prerequisites"
    if facts.to_confirm:
        return "confirmations"
    return None


_NEW_FLEET = (
    "No fleet yet: set one up with the user, one question at a time, never all at once.\n"
    "1. What the fleet is for: the outcome it delivers and for whom (its name and description).\n"
    "2. The agents it needs: reuse the user's agents with no home fleet (agent tools list them) or new ones "
    "with a name, a description and a persona.\n"
    "3. The orchestrator: the home member that takes the work in.\n"
    "4. The workspace: an existing one the user manages, or a new one named after the fleet.\n"
    "5. The integrations and knowledge its agents need.\n"
    "A good default to suggest: every member also holds a consult handover to the orchestrator, so it can "
    "ask it a route, scope or context question.\n"
    "Another good default to suggest at step 5, once, in one question: one shared working knowledge base "
    "named after the fleet followed by ' - Working knowledge', written by the orchestrator and read by every member; "
    "get_fleet_setup on the stored fleet lists its skeleton documents.\n"
    "Then propose the whole configuration with generate_fleet_spec (a dry run, nothing written), and apply it "
    "with apply_fleet_spec(dry_run=false) only once the user confirmed. Read it back with "
    "get_fleet_setup(fleet_id) and continue with the step it names."
)

_ASK = {
    "purpose": "Ask what the fleet is for (the outcome and for whom), propose a one-paragraph description, "
    "and save it with update_fleet once the user confirmed.",
    "members": "Ask which agents the fleet needs; propose existing agents with no home fleet or new ones, "
    "and add them with set_fleet_members once the user confirmed.",
    "orchestrator": "Ask which home member takes the work in, propose one, and set it with "
    "update_fleet(orchestrator=...) once the user confirmed.",
    "prerequisites": "Walk the user through each prerequisite not met, one at a time: say where it is fixed "
    "(the fix route of get_fleet_prerequisites); integrations and knowledge bases are connected by the user.",
    "confirmations": "Ask the user to confirm each manual prerequisite, one at a time, and record each answer "
    "with confirm_fleet_prerequisite.",
}


def render_setup(facts: FleetSetupFacts | None, *, fleet_id: str = "", link: str = "") -> str:
    """The answer of ``get_fleet_setup``: where the fleet stands and what to ask next."""
    if facts is None:
        return _NEW_FLEET
    step = next_step(facts)
    done = SETUP_STEPS.index(step) if step else len(SETUP_STEPS)
    lines = [f"Fleet **{facts.name}** (`{fleet_id}`): setup {done}/{len(SETUP_STEPS)} steps done."]
    if link:
        lines.append(f"  • Page: {link}")
    lines.append(f"  • Description: {'set' if facts.description.strip() else 'missing'}")
    lines.append(f"  • Home members: {facts.home_members}")
    lines.append(f"  • Orchestrator: {'set' if facts.has_orchestrator else 'missing'}")
    if facts.unmet:
        lines.append("  • Prerequisites not met: " + "; ".join(facts.unmet))
    if facts.to_confirm:
        lines.append("  • To confirm: " + "; ".join(facts.to_confirm))
    if facts.has_orchestrator and facts.cannot_consult_orchestrator:
        lines.append(
            "  • Suggestion (optional, not a setup step): "
            + ", ".join(facts.cannot_consult_orchestrator)
            + " cannot consult the orchestrator. Propose once, in one question, to give each a consult "
            "handover to it (route, scope and context questions); apply only on the user's yes."
        )
    if facts.has_orchestrator and facts.without_working_knowledge:
        skeleton = "; ".join(f"'{title}' ({what})" for title, what in WORKING_KNOWLEDGE_SKELETON)
        lines.append(
            "  • Suggestion (optional, not a setup step): the fleet has no working knowledge base. Propose once, "
            f"in one question, to create '{working_knowledge_name(facts.name)}', written by the orchestrator and "
            "read by every member (one writer, every member starts from the same facts). Only on the user's yes: "
            "create_knowledge_base, add_kb_document for each skeleton document, then update_agent with "
            "knowledge_base_ids on every home member and knowledge_base_permissions (the new knowledge base's id "
            f"followed by ':write') on the orchestrator. Skeleton: {skeleton}. Skill 'Fleet design and operations', section 9."
        )
    if step is None:
        lines.append("The setup is complete. End with a one-line summary of the fleet and its page; ask nothing more.")
    else:
        lines.append(f"Next step: {step}. {_ASK[step]} Then call get_fleet_setup again.")
    return "\n".join(lines)
