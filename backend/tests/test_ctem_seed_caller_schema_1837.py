"""#1837: the seeded CTEM Threat Relevance Analyst accepts the caller's output_schema.

The loop and the use-case orchestrators call it with their own scoring schema; with
``accept_caller_output_format`` off the call was refused before the analyst ran. The
seed writes the flag at every boot, so an existing deployment takes it.
"""

from app.agents.builtin_agents import BUILTIN_AGENTS
from app.agents.seed import _sync_agent_fields
from app.models.agent import Agent


def _seed(slug: str) -> dict:
    (data,) = [a for a in BUILTIN_AGENTS if a["slug"] == slug]
    return data


def test_relevance_analyst_declares_it_accepts_the_caller_schema():
    assert _seed("ctem-threat-relevance-analyst").get("accept_caller_output_format") is True


def test_the_seed_writes_the_flag_on_an_existing_agent():
    agent = Agent(slug="ctem-threat-relevance-analyst", name="x", accept_caller_output_format=False)
    _sync_agent_fields(agent, _seed("ctem-threat-relevance-analyst"))
    assert agent.accept_caller_output_format is True


def test_the_seed_turns_it_off_where_the_definition_does_not_ask():
    agent = Agent(slug="ctem-risk-quantifier", name="x", accept_caller_output_format=True)
    _sync_agent_fields(agent, _seed("ctem-risk-quantifier"))
    assert agent.accept_caller_output_format is False
