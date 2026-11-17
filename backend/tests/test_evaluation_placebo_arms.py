"""The runs of a placebo comparison and what each arm changes (level 2).

``dev-docs/agent-placebo-evaluation.md`` FR-3, FR-14, FR-15, FR-16, FR-17: which
targets an agent can take, one run per arm x trial in an interleaved order
fixed by the seed, a ``skill`` target resolved among the agent's own skills,
the tool sources a confirmation lists, the overrides a control arm sends, and
the reuse fingerprint of a control run.
"""

from __future__ import annotations

import uuid
from collections import Counter
from types import SimpleNamespace

import pytest

from app.services.evaluation.fingerprint import compute_input_fingerprint
from app.services.evaluation.placebo_arms import (
    ARM_NO_TARGET,
    ARM_PLACEBO,
    ARM_REAL,
    TargetSources,
    placebo_seed,
    plan_comparison,
    target_sources,
    write_tool_sources,
)
from app.services.evaluation.placebo_runs import control_arm_overrides
from app.services.evaluation_placebo import PlaceboConfig, PlaceboSource, PlaceboTarget

SKILL_A = str(uuid.uuid4())
SKILL_B = str(uuid.uuid4())
SOURCES = TargetSources(
    persona="You are an analyst.",
    prompts=(PlaceboSource(content="Use short answers.", name="Style", ref_id="p1"),),
    skills=(
        PlaceboSource(content="Body A", name="Triage", ref_id=SKILL_A),
        PlaceboSource(content="Body B", name="Report", ref_id=SKILL_B, always_load=True),
    ),
)


def _config(**fields) -> PlaceboConfig:
    return PlaceboConfig.model_validate({"targets": [{"kind": "persona"}], **fields})


@pytest.mark.parametrize(
    ("target", "sources", "expected"),
    [
        ({"kind": "persona"}, SOURCES, (["You are an analyst."], None)),
        ({"kind": "persona"}, TargetSources(persona="  "), ([], "the agent has no persona")),
        ({"kind": "prompts"}, SOURCES, (["Use short answers."], None)),
        ({"kind": "prompts"}, TargetSources(persona="x"), ([], "the agent has no attached Prompt")),
        ({"kind": "skills"}, SOURCES, (["Body A", "Body B"], None)),
        ({"kind": "skills"}, TargetSources(persona="x"), ([], "the agent has no skill it can load")),
        ({"kind": "skill", "skill_id": SKILL_B}, SOURCES, (["Body B"], None)),
        (
            {"kind": "skill", "skill_id": str(uuid.uuid4())},
            SOURCES,
            ([], "the skill is not bound to the agent, or the agent cannot load it"),
        ),
    ],
    ids=[
        "persona", "no-persona", "prompts", "no-prompt", "skills", "no-skill", "one-skill",
        "a-skill-the-agent-does-not-have",
    ],
)
def test_a_target_takes_what_this_agent_has(target, sources, expected):
    elements, reason = target_sources(PlaceboTarget.model_validate(target), sources)

    assert ([element.content for element in elements], reason) == expected


def test_a_plan_has_one_run_per_arm_and_trial_and_notes_what_an_agent_lacks():
    config = _config(
        targets=[{"kind": "persona"}, {"kind": "prompts"}], trials_per_entry=3, include_no_target=True
    )
    agents = [
        ("agent-1", "One", ["m1", "m2"], SOURCES),
        ("agent-2", "Two", ["m1"], TargetSources(persona="Another persona.")),
    ]

    plan = plan_comparison(config, agents, seed="seed-1")

    # agent-1: 2 models x (real + 2 targets x (placebo + no target)) = 10 arms;
    # agent-2: 1 model x (real + persona x 2) = 3 arms. Three trials each.
    assert len(plan.runs) == (10 + 3) * 3
    assert Counter(run.trial for run in plan.runs) == {0: 13, 1: 13, 2: 13}
    assert Counter(run.arm for run in plan.runs) == {ARM_REAL: 9, ARM_PLACEBO: 15, ARM_NO_TARGET: 15}
    assert plan.skipped == [
        {"agent_id": "agent-2", "agent_name": "Two", "target": "prompts", "reason": "the agent has no attached Prompt"}
    ]
    assert plan.executions(12) == 39 * 12


def test_runs_come_trial_by_trial_with_arms_in_an_order_the_seed_fixes():
    config = _config(targets=[{"kind": "persona"}, {"kind": "skills"}], trials_per_entry=4)
    agents = [("agent-1", "One", ["m1"], SOURCES)]

    plan = plan_comparison(config, agents, seed="seed-1")
    again = plan_comparison(config, agents, seed="seed-1")
    other = plan_comparison(config, agents, seed="seed-2")

    trials = [run.trial for run in plan.runs]
    assert trials == sorted(trials)
    assert plan.runs == again.runs
    by_trial = [[(run.arm, str(run.target)) for run in plan.runs if run.trial == trial] for trial in range(4)]
    assert len({tuple(order) for order in by_trial}) > 1
    assert plan.runs != other.runs


def test_without_the_no_target_arm_only_real_and_placebo_run():
    plan = plan_comparison(
        _config(include_no_target=False, trials_per_entry=1), [("a", "A", ["m"], SOURCES)], seed="s"
    )

    assert sorted(run.arm for run in plan.runs) == [ARM_PLACEBO, ARM_REAL]


def test_a_placebo_seed_is_the_same_for_every_model_and_trial_of_one_agent_and_target():
    assert placebo_seed("c", "agent-1", "persona") == "c:agent-1:persona"
    assert placebo_seed("c", "agent-1", "persona") != placebo_seed("c", "agent-2", "persona")


def test_the_tool_sources_a_confirmation_lists():
    agent = SimpleNamespace(
        integration_bindings={"opencti": {}, "abuseipdb": {}},
        mcp_server_ids=["m1", "m2"],
        custom_tool_ids=["c1"],
        builtin_tool_ids=["web_search"],
    )

    assert write_tool_sources(agent) == [
        "integration abuseipdb",
        "integration opencti",
        "2 MCP server(s)",
        "1 custom tool(s)",
        "built-in web_search",
    ]
    assert write_tool_sources(SimpleNamespace()) == []


def _run(arm: str, target: dict, snapshot: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(arm=arm, placebo_target=target, placebo_snapshot=snapshot)


_AGENT = SimpleNamespace(skill_ids=[SKILL_A, SKILL_B])
_SNAPSHOT = {
    "elements": [
        {"name": "Notes on files", "description": "Background.", "content": "NEUTRAL", "ref_id": SKILL_A},
    ]
}


@pytest.mark.parametrize(
    ("run", "expected"),
    [
        (_run(ARM_NO_TARGET, {"kind": "persona"}), {"persona": ""}),
        (_run(ARM_NO_TARGET, {"kind": "prompts"}), {"prompt_fragment": ""}),
        (_run(ARM_NO_TARGET, {"kind": "skills"}), {"skill_ids_removed": [SKILL_A, SKILL_B]}),
        (_run(ARM_NO_TARGET, {"kind": "skill", "skill_id": SKILL_B}), {"skill_ids_removed": [SKILL_B]}),
        (
            _run(ARM_PLACEBO, {"kind": "persona"}, {"elements": [{"content": "NEUTRAL PERSONA"}]}),
            {"persona": "NEUTRAL PERSONA"},
        ),
    ],
    ids=["no-persona", "no-prompts", "no-skills", "no-skill", "placebo-persona"],
)
def test_a_control_arm_removes_or_replaces_its_target(run, expected):
    assert control_arm_overrides(run, _AGENT, run.placebo_snapshot) == expected


def test_a_placebo_prompt_goes_through_the_prompt_header_and_a_placebo_skill_through_a_substitute():
    prompts = control_arm_overrides(_run(ARM_PLACEBO, {"kind": "prompts"}, _SNAPSHOT), _AGENT, _SNAPSHOT)
    skills = control_arm_overrides(_run(ARM_PLACEBO, {"kind": "skills"}, _SNAPSHOT), _AGENT, _SNAPSHOT)

    assert prompts == {"prompt_fragment": "\n── Attached Prompts ──\n\n### Notes on files\nBackground.\nNEUTRAL"}
    (substitute,) = skills["skill_substitutes"].values()
    assert list(skills["skill_substitutes"]) == [SKILL_A]
    assert (substitute.name, substitute.description, substitute.content) == ("Notes on files", "Background.", "NEUTRAL")


def test_a_control_run_has_a_fingerprint_of_its_own_and_every_other_run_keeps_its_own():
    base = {"agent_id": "a", "model_id": "m", "execution_mode": "full_loop", "entry_input": "Q"}
    placebo = {"arm": ARM_PLACEBO, "trial": 0, "placebo": _SNAPSHOT}

    real = compute_input_fingerprint(**base)

    assert compute_input_fingerprint(**base, placebo=None) == real
    assert compute_input_fingerprint(**base, placebo=placebo) != real
    assert compute_input_fingerprint(**base, placebo=placebo | {"trial": 1}) != compute_input_fingerprint(
        **base, placebo=placebo
    )
