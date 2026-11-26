# SPDX-License-Identifier: Apache-2.0

"""A rejected or ticketed validation gate also turns the coverage off and decides the Grouping (#2470)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.agents import builtin_agents as ba

_PERSONAS = Path(ba.__file__).parent / "seed_data" / "agent_persona"
# The validation step of each CTEM orchestrator, the one that files the ctem.validation.non_readonly gate.
_STEPS = {
    "ctem-threat-response-orchestrator.md": ("3. **Validation.**", "4. **Risk.**"),
    "ctem-vuln-prioritization-orchestrator.md": ("3. **Validate**", "4. **Quantify and decide**"),
}


def _step(name: str) -> str:
    text = (_PERSONAS / name).read_text(encoding="utf-8")
    start, end = _STEPS[name]
    return text[text.index(start) : text.index(end)]


@pytest.mark.parametrize("name", sorted(_STEPS))
def test_rejection_turns_the_coverage_off_after_the_scenario_and_reads_it_back(name: str) -> None:
    step = _step(name)
    order = [
        step.index("`delete_openaev_scenario`"),
        step.index("`UNVERIFIED`"),
        step.index("`manage_opencti_security_coverage` `update` with `auto_enrichment_disable` true"),
    ]
    assert order == sorted(order)
    assert "(or `delete` when the chosen option deletes it), read back with `get`" in step
    assert "OpenAEV never rebuilds the scenario from a later coverage update" in step


@pytest.mark.parametrize("name", sorted(_STEPS))
def test_the_grouping_is_kept_by_default_and_deleted_only_on_the_chosen_option(name: str) -> None:
    step = _step(name)
    assert "the Grouping (threat package) is kept by default" in step
    assert "deleted (`delete_opencti_entity`) only when the chosen option says so" in step


@pytest.mark.parametrize("name", sorted(_STEPS))
def test_each_option_names_its_outcome_and_the_cleanup_is_written_on_the_case(name: str) -> None:
    step = _step(name)
    assert (
        "names in its own text what it does to the scenario, its arsenal actions, the coverage and the Grouping" in step
    )
    assert "turn the coverage's auto enrichment off, keep the Grouping" in step
    assert "the cleanup is written on the case with `update_ctem_case` `note`" in step


@pytest.mark.parametrize("slug", ["ctem-threat-response-orchestrator", "ctem-vuln-prioritization-orchestrator"])
def test_the_orchestrators_hold_the_cleanup_tools(slug: str) -> None:
    agent = next(a for a in ba.BUILTIN_AGENTS if a["slug"] == slug)
    assert "opencti" in agent["integration_template_bindings"]
    denied = set(agent.get("tool_deny_list") or ())
    assert not denied & {"manage_opencti_security_coverage", "delete_opencti_entity"}
