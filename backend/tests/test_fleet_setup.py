"""The conversational setup of a fleet (#11250): the next step derived from the fleet as stored."""

from __future__ import annotations

import pytest

from app.services.builtin_tools import fleets as fleet_tools
from app.services.fleet_setup import SETUP_STEPS, FleetSetupFacts, can_consult, next_step, render_setup

_COMPLETE = FleetSetupFacts(
    name="Desk", description="Triage alerts for the SOC.", home_members=2, has_orchestrator=True
)


@pytest.mark.parametrize(
    ("facts", "expected"),
    [
        (FleetSetupFacts(name="Desk"), "purpose"),
        (FleetSetupFacts(name="Desk", description="   "), "purpose"),
        (FleetSetupFacts(name="Desk", description="SOC triage"), "members"),
        (FleetSetupFacts(name="Desk", description="SOC triage", home_members=2), "orchestrator"),
        (
            FleetSetupFacts(
                name="Desk", description="SOC triage", home_members=2, has_orchestrator=True, unmet=["OpenCTI"]
            ),
            "prerequisites",
        ),
        (
            FleetSetupFacts(
                name="Desk", description="SOC triage", home_members=2, has_orchestrator=True, to_confirm=["A runbook"]
            ),
            "confirmations",
        ),
        (_COMPLETE, None),
    ],
)
def test_next_step_follows_the_setup_order(facts, expected):
    assert next_step(facts) == expected


def test_the_steps_are_the_ones_asked_in_order():
    assert SETUP_STEPS == ("purpose", "members", "orchestrator", "prerequisites", "confirmations")


def test_a_new_fleet_gets_the_interview_and_writes_only_on_confirmation():
    text = render_setup(None)
    assert "one question at a time" in text
    assert "generate_fleet_spec" in text
    assert "apply_fleet_spec(dry_run=false) only once the user confirmed" in text


def test_a_fleet_in_progress_names_its_next_step_and_its_page():
    facts = FleetSetupFacts(name="Desk", description="SOC triage", home_members=1)
    text = render_setup(facts, fleet_id="f-1", link="https://x/fleets/f-1")
    assert "setup 2/5 steps done" in text
    assert "Page: https://x/fleets/f-1" in text
    assert "Next step: orchestrator." in text
    assert "update_fleet(orchestrator=...)" in text


def test_unmet_prerequisites_are_listed_before_the_confirmations():
    facts = FleetSetupFacts(
        name="Desk",
        description="SOC triage",
        home_members=2,
        has_orchestrator=True,
        unmet=["OpenCTI"],
        to_confirm=["A runbook"],
    )
    text = render_setup(facts, fleet_id="f-1")
    assert "Prerequisites not met: OpenCTI" in text
    assert "To confirm: A runbook" in text
    assert "Next step: prerequisites." in text


def test_a_complete_fleet_ends_the_interview():
    text = render_setup(_COMPLETE, fleet_id="f-1")
    assert "setup 5/5 steps done" in text
    assert "The setup is complete." in text
    assert "Next step" not in text


def test_the_back_consult_is_a_suggestion_never_a_step():
    """#11948: members that cannot consult the orchestrator are proposed once, never a setup step."""
    facts = FleetSetupFacts(
        name="Desk",
        description="SOC triage",
        home_members=3,
        has_orchestrator=True,
        cannot_consult_orchestrator=["Enrich", "Triage"],
    )
    assert next_step(facts) is None
    text = render_setup(facts, fleet_id="f-1")
    assert "setup 5/5 steps done" in text
    assert "Suggestion (optional, not a setup step): Enrich, Triage cannot consult the orchestrator." in text
    assert "apply only on the user's yes" in text
    assert "Suggestion" not in render_setup(_COMPLETE, fleet_id="f-1")
    assert "consult handover to the orchestrator" in render_setup(None)


def test_the_working_knowledge_base_is_a_suggestion_never_a_step():
    """#11972: a fleet with no working knowledge base is proposed one once, with its skeleton, never a step."""
    from app.services.fleet_setup import WORKING_KNOWLEDGE_SKELETON

    facts = FleetSetupFacts(
        name="Alert Desk",
        description="SOC triage",
        home_members=4,
        has_orchestrator=True,
        without_working_knowledge=True,
    )
    assert next_step(facts) is None
    text = render_setup(facts, fleet_id="f-1")
    assert "setup 5/5 steps done" in text
    assert "the fleet has no working knowledge base" in text
    assert "'Alert Desk - Working knowledge'" in text
    assert "Only on the user's yes" in text
    assert len(WORKING_KNOWLEDGE_SKELETON) == 5
    for title, _what in WORKING_KNOWLEDGE_SKELETON:
        assert title in text
    assert "working knowledge" not in render_setup(_COMPLETE, fleet_id="f-1")
    assert "Working knowledge" in render_setup(None)


@pytest.mark.parametrize(
    ("ids", "types", "expected"),
    [
        (["o"], {"o": "consult"}, True),
        (["o"], {}, False),
        (["o"], {"o": "delegate"}, False),
        (["x"], {"o": "consult"}, False),
        (None, None, False),
    ],
)
def test_can_consult_reads_the_handover_type(ids, types, expected):
    assert can_consult(ids, types, "o") is expected


def test_the_setup_tool_is_offered_without_load_tools():
    """The interview opens with get_fleet_setup: it is a core function of the
    platform group, never deferred behind load_tools."""
    from app.services.builtin_tool_groups import BUILTIN_TOOL_GROUPS

    platform = next(group for group in BUILTIN_TOOL_GROUPS if group.id == "platform")
    assert platform.core is not None
    assert {"get_fleet_setup", "create_fleet"} <= set(platform.core)


async def test_the_tool_without_a_fleet_answers_the_new_fleet_interview():
    _defs, executor = fleet_tools.build_fleet_tools("")
    answer = await executor.handler("get_fleet_setup")()
    assert answer == render_setup(None)


async def test_the_tool_needs_an_account_for_an_existing_fleet():
    _defs, executor = fleet_tools.build_fleet_tools("")
    answer = await executor.handler("get_fleet_setup")(fleet_id="00000000-0000-0000-0000-000000000001")
    assert answer == fleet_tools._ANONYMOUS_REFUSAL
