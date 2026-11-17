"""The iteration budgets the built-in agents ship with.

The five product assistants run on the platform default
(``DEFAULT_MAX_ITERATIONS``); the CTEM Assistant is the General Assistant in
XTM One mode. Every other built-in that can call something - a built-in tool,
an integration, a handover peer or its memory - gets at least 100 rounds. A cap
below that is kept only where it bounds a single model answer (the agent has
nothing to call), or where the agent's definition documents why it is bounded.
"""

from __future__ import annotations

from app.agents.builtin_agents import BUILTIN_AGENTS
from app.models.general_assistant_config import DEFAULT_MAX_ITERATIONS

_ASSISTANTS = (
    "ctem-assistant",
    "xtm-one-assistant",
    "opencti-assistant",
    "openaev-assistant",
    "opencrq-assistant",
)

_FLOOR = 100

# Bounded on purpose; the reason is written next to the cap in the definition.
_DOCUMENTED_BOUNDS = {
    # The investigation is the tool's own bounded loop.
    "deep-investigation-agent",
    # A bounded repair loop, see test_cti_stix_harvester_config.py.
    "cti-stix-harvester",
}


def _by_slug() -> dict[str, dict]:
    return {agent["slug"]: agent for agent in BUILTIN_AGENTS}


def _can_call_something(agent: dict) -> bool:
    return bool(
        agent.get("builtin_tool_ids")
        or agent.get("integration_template_bindings")
        or agent.get("handover_agent_slugs")
        or agent.get("memory_persistent_enabled", True)
    )


def test_the_platform_default_is_300():
    assert DEFAULT_MAX_ITERATIONS == 300


def test_the_five_assistants_run_on_the_platform_default():
    agents = _by_slug()
    assert {slug: agents[slug].get("max_iterations") for slug in _ASSISTANTS} == {
        slug: DEFAULT_MAX_ITERATIONS for slug in _ASSISTANTS
    }


def test_a_builtin_below_the_floor_has_nothing_to_call():
    offenders = sorted(
        agent["slug"]
        for agent in BUILTIN_AGENTS
        if agent.get("max_iterations") is not None
        and agent["max_iterations"] < _FLOOR
        and agent["slug"] not in _DOCUMENTED_BOUNDS
        and _can_call_something(agent)
    )
    assert not offenders, (
        f"built-in agents that can call tools are capped below {_FLOOR} "
        f"iterations: {offenders}. Raise them to at least {_FLOOR}, or leave "
        "max_iterations out to inherit the platform default."
    )


def test_every_documented_bound_is_still_a_bound():
    agents = _by_slug()
    assert {slug: agents[slug]["max_iterations"] < _FLOOR for slug in _DOCUMENTED_BOUNDS} == {
        slug: True for slug in _DOCUMENTED_BOUNDS
    }
