"""Seeded CTEM agents keep no per-user memory (#1837, gap ctem-seed-agent-hygiene).

A CTEM agent is company-managed and shared by every person of the organization:
a fact one person's run writes into the cross-agent user memory must not steer
another person's case. Every seeded agent tagged ``ctem`` turns that tier off.
"""

from app.agents.builtin_agents import BUILTIN_AGENTS


def _ctem_seeds() -> list[dict]:
    return [agent for agent in BUILTIN_AGENTS if "ctem" in (agent.get("tags") or [])]


def test_there_are_ctem_seeds() -> None:
    assert len(_ctem_seeds()) >= 10


def test_every_ctem_seed_turns_user_memory_off() -> None:
    on = sorted(agent["slug"] for agent in _ctem_seeds() if agent.get("memory_user_enabled", True))
    assert on == []
